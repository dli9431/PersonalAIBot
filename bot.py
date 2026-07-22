#!/usr/bin/env python3
"""
Discord → Claude Code / Codex CLI / Kimi Code CLI → Git bridge bot.

Supports iterative sessions: send a task, review changes, send follow-ups,
and only commit when you're satisfied. Uses --resume (Claude Code), -c
(Kimi Code), and explicit Codex thread resumes for multi-turn context.

Designed to run on Linux (including WSL2).

Requirements:
    pip install discord.py python-dotenv
    Optional: gh CLI (for PR creation)
"""

import asyncio
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time
import tomllib
import urllib.request

import discord
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
REPO_PATH = os.environ["REPO_PATH"]
BRANCH_PREFIX = os.getenv("BRANCH_PREFIX", "auto")
MAIN_BRANCH = os.getenv("MAIN_BRANCH", "main")
DEV_BRANCH = os.getenv("DEV_BRANCH", "dev")
PROTECTED_BRANCHES_ENV = os.getenv("PROTECTED_BRANCHES", "")
MAX_DIFF_CHARS = 1800
REVIEW_MESSAGE_LIMIT = 1900
REVIEW_CODE_CHUNK_LIMIT = 600
PULL_SUMMARY_COMMIT_LIMIT = 5
PULL_SUMMARY_FILE_LIMIT = 30
GIT_NETWORK_TIMEOUT = 300  # fetch/pull deadline; engine turns themselves do not expire

DEFAULT_ENGINE = os.getenv("DEFAULT_ENGINE", "claude")

# Claude Code (global fallback defaults; channels can override at runtime)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "sonnet")
CLAUDE_REASONING_EFFORT = os.getenv("CLAUDE_REASONING_EFFORT", "").strip().lower() or None
CLAUDE_ALLOWED_TOOLS = os.getenv("CLAUDE_ALLOWED_TOOLS",
    "Read Edit Write Grep Glob LS Bash(git\\ diff) Bash(git\\ status)"
).split()
CLAUDE_DENIED_TOOLS = os.getenv("CLAUDE_DENIED_TOOLS",
    "Bash(rm\\ *) Bash(sudo\\ *) Bash(curl\\ *) Bash(wget\\ *) WebFetch"
).split()

# Codex CLI
CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.5")
CODEX_REASONING_EFFORT = os.getenv("CODEX_REASONING_EFFORT", "").strip().lower() or None

# Kimi Code CLI
KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-code/k3")
KIMI_REASONING_EFFORT = os.getenv("KIMI_REASONING_EFFORT", "").strip().lower() or None

CLAUDE_REASONING_LEVELS = ("low", "medium", "high")
CODEX_REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
KIMI_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_REASONING_OPTIONS = (*CLAUDE_REASONING_LEVELS, "default")
CODEX_REASONING_OPTIONS = (*CODEX_REASONING_LEVELS, "default")
KIMI_REASONING_OPTIONS = (*KIMI_REASONING_LEVELS, "default")

CONTEXT_MAX_CHARS = int(os.getenv("CONTEXT_MAX_CHARS", "4000"))
PLAN_CONTEXT_MAX_CHARS = int(os.getenv("PLAN_CONTEXT_MAX_CHARS", "12000"))

# Additional git repos the bot can commit/push to.
# Format: comma-separated paths (absolute, or relative to REPO_PATH).
# Project 1 is always REPO_PATH itself.
def _load_git_projects() -> list[tuple[str, str]]:
    """Return list of (label, absolute_path) tuples. Index 0 = project 1."""
    projects = [(pathlib.Path(REPO_PATH).name, REPO_PATH)]
    raw = os.getenv("GIT_PROJECTS", "")
    for entry in (e.strip() for e in raw.split(",") if e.strip()):
        if ":" in entry:
            name, path = entry.split(":", 1)
        else:
            path = entry
            name = pathlib.Path(path).name
        abs_path = path if pathlib.Path(path).is_absolute() else str(pathlib.Path(REPO_PATH) / path)
        projects.append((name.strip(), abs_path))
    return projects

GIT_PROJECTS: list[tuple[str, str]] = _load_git_projects()

_BASE_DIR = pathlib.Path(__file__).resolve().parent
_STATE_ENV = os.getenv("BOT_STATE_FILE", "")
STATE_FILE = pathlib.Path(_STATE_ENV) if _STATE_ENV else (_BASE_DIR / ".bot_state.json")
if not STATE_FILE.is_absolute():
    STATE_FILE = _BASE_DIR / STATE_FILE

# Track login processes so we don't run two at once
_login_lock: dict[int, bool] = {}  # channel_id → True while login in progress
_restart_on_close = False

# channel_id → asyncio.Event used to cancel current engine run
stop_events: dict[int, asyncio.Event] = {}
# channel_id → running subprocess for engine
running_procs: dict[int, asyncio.subprocess.Process] = {}
# channel_id → currently running task metadata (engine/cwd/task/run_id)
active_run_contexts: dict[int, dict] = {}
_RESTART_FLAG = pathlib.Path("/tmp/bot_restart_channel")

# ── Discord client setup ─────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)
logger = logging.getLogger(__name__)


# ── Session state ─────────────────────────────────────────────────────────────
# An active session means we're on a feature branch, iterating with an engine.
# channel_id → session dict
active_sessions: dict[int, dict] = {}
# channel_id → last pushed branch name (for merge/PR commands)
last_pushed: dict[int, str] = {}
# channel_id → active working directory (defaults to REPO_PATH)
channel_cwd: dict[int, str] = {}
# channel_id → numbered branch list from last `branches` command
branch_listing: dict[int, list[str]] = {}
# channel_id → runtime config override (engine/model/reasoning)
CHANNEL_RUNTIME_CONFIGS: dict[int, dict] = {}
# channel_id → token usage dict from last engine run (includes "engine")
channel_last_usage: dict[int, dict] = {}
# canonical repo path → lock for shared integration operations. Engine runs stay
# concurrent in channel worktrees; only merges into a shared target serialize.
_repo_integration_locks: dict[str, asyncio.Lock] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_authorised(msg: discord.Message) -> bool:
    return msg.author.id == ALLOWED_USER_ID


def slugify(text: str, max_len: int = 40) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in text.lower())
    return (slug.strip("-")[:max_len].rstrip("-")) or "task"


def run_git(
    cmd: list[str],
    path: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=path or REPO_PATH, capture_output=True, text=True, timeout=timeout,
    )


def run_git_in(cmd: list[str], path: str) -> subprocess.CompletedProcess:
    return run_git(cmd, path)


def resolve_project(arg: str) -> tuple[str, str] | None:
    """Resolve '1', '2', or a name substring to (label, path). None if not found."""
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(GIT_PROJECTS):
            return GIT_PROJECTS[idx]
        return None
    for label, path in GIT_PROJECTS:
        if arg.lower() in label.lower():
            return label, path
    return None


def resolve_branch(ref: str, ch_id: int, cwd: str | None = None) -> str | None:
    """Resolve a branch reference: N (or #N) from branch_listing, or name.

    Returns the branch name, or None if not found.
    """
    # Strip leading # if present
    clean = ref.lstrip("#")
    # Try as a number index into the cached branch listing
    if clean.isdigit():
        idx = int(clean) - 1
        listing = branch_listing.get(ch_id, [])
        if 0 <= idx < len(listing):
            return listing[idx]
        return None
    # Otherwise treat as a literal branch name
    return ref


def get_branch_list(cwd: str | None = None) -> list[str]:
    """Return recent branches sorted by committer date."""
    result = run_git(["git", "branch", "--sort=-committerdate",
                      "--format=%(refname:short)"], cwd)
    return [b for b in result.stdout.strip().split("\n") if b]


def resolve_branch_case_insensitive(name: str, cwd: str | None = None) -> str | None:
    """Resolve branch name ignoring case, if unambiguous."""
    if not name:
        return None
    branches = get_branch_list(cwd)
    if name in branches:
        return name
    matches = [b for b in branches if b.lower() == name.lower()]
    if len(matches) == 1:
        return matches[0]
    return None


def expand_branch_args(tokens: list[str], ch_id: int, cwd: str) -> list[str]:
    """Expand branch tokens, resolving N/#N references and splitting commas."""
    names: list[str] = []
    for token in tokens:
        for chunk in token.split(","):
            name = chunk.strip()
            if not name:
                continue
            resolved = resolve_branch(name, ch_id, cwd) if name.lstrip("#").isdigit() else None
            names.append(resolved or name)
    return names


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_HUNK_CONTEXT_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def truncate(text: str, limit: int = MAX_DIFF_CHARS) -> str:
    if len(text) <= limit:
        return text
    h = limit // 2 - 20
    return text[:h] + "\n\n... (truncated) ...\n\n" + text[-h:]


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _sanitize_discord_text(text: str) -> str:
    clean = strip_ansi(text or "")
    return "".join(ch for ch in clean if ch in ("\n", "\t") or ord(ch) >= 32)


def _sanitize_code_block_text(text: str) -> str:
    clean = _sanitize_discord_text(text or "(none)") or "(none)"
    return clean.replace("```", "'''")


def _split_text_for_code_block(text: str, max_chunk_chars: int) -> list[str]:
    """Split text into chunks suitable for fenced code blocks without truncating content."""
    max_chunk = max(80, max_chunk_chars)
    lines = (text or "").splitlines()
    if not lines:
        return ["(none)"]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in lines:
        raw_line = line
        while len(raw_line) > max_chunk:
            segment = raw_line[:max_chunk]
            raw_line = raw_line[max_chunk:]
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_len = 0
            chunks.append(segment)
        line_len = len(raw_line) + 1
        if current_lines and current_len + line_len > max_chunk:
            chunks.append("\n".join(current_lines))
            current_lines = [raw_line]
            current_len = line_len
        else:
            current_lines.append(raw_line)
            current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks or ["(none)"]


def _record_session_followup(session: dict, instruction: str) -> None:
    note = _collapse_whitespace(instruction)
    if not note or note.lower() == "describe and analyze these images":
        return
    followups = session.setdefault("followups", [])
    if not isinstance(followups, list):
        followups = []
        session["followups"] = followups
    if note in followups:
        return
    followups.append(note)
    if len(followups) > 12:
        del followups[:-12]


def _session_intent_summary(session: dict | None, limit: int = 260) -> str:
    if not session:
        return ""
    parts: list[str] = []
    initial = _collapse_whitespace(str(session.get("description") or ""))
    if initial:
        parts.append(initial)
    followups = session.get("followups")
    if isinstance(followups, list):
        for followup in followups:
            note = _collapse_whitespace(str(followup or ""))
            if note and note not in parts:
                parts.append(note)
    if not parts:
        return ""
    summary = " | ".join(parts)
    return truncate(summary, limit).replace("\n", " ")


def _clean_string_list(values: object, limit: int | None = None) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = [_collapse_whitespace(str(value or "")) for value in values]
    result = [value for value in cleaned if value]
    if limit is not None and len(result) > limit:
        return result[-limit:]
    return result


def _coerce_usage_totals(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
        raw = value.get(key)
        try:
            num = int(raw)
        except (TypeError, ValueError):
            continue
        if num:
            result[key] = num
    return result


def current_branch(path: str | None = None) -> str:
    return run_git(["git", "branch", "--show-current"], path).stdout.strip()


def _parse_branch_list(raw: str) -> list[str]:
    if not raw:
        return []
    return [b.strip() for b in re.split(r"[,\s]+", raw) if b.strip()]


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict) -> None:
    temp_path = STATE_FILE.with_name(
        f".{STATE_FILE.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temp_path.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.replace(temp_path, STATE_FILE)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _accumulate_global_usage(engine: str, usage: dict) -> None:
    """Add token counts from one run to persistent global usage stats."""
    data = _load_state()
    stats = data.setdefault("usage_stats", {})
    eng = stats.setdefault(engine, {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read": 0, "cache_write": 0, "runs": 0,
    })
    eng["input_tokens"] += usage.get("input_tokens", 0)
    eng["output_tokens"] += usage.get("output_tokens", 0)
    eng["cache_read"] += usage.get("cache_read", 0)
    eng["cache_write"] += usage.get("cache_write", 0)
    eng["runs"] += 1
    _save_state(data)


def get_global_usage_stats() -> dict:
    """Return persistent cumulative usage stats from bot state."""
    return _load_state().get("usage_stats", {})


def _absorb_usage_into_session(session: dict, ch_id: int) -> None:
    """Accumulate the last engine run's token usage into the session's running total."""
    last = channel_last_usage.get(ch_id, {})
    if not last:
        return
    if _normalize_engine_name(last.get("engine")) != _normalize_engine_name(session.get("engine")):
        return
    codex_thread_id = _clean_codex_thread_id(last.get("codex_thread_id"))
    if _normalize_engine_name(session.get("engine")) == "codex" and codex_thread_id:
        session["codex_thread_id"] = codex_thread_id
    total = session.setdefault("total_usage", {})
    for key in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
        total[key] = total.get(key, 0) + last.get(key, 0)


PROTECTED_BRANCHES: list[str] = []
_PROTECTED_BRANCH_KEYS: set[str] = set()


def _default_protected_branches() -> list[str]:
    defaults = [b for b in (MAIN_BRANCH, DEV_BRANCH) if b]
    defaults.extend(_parse_branch_list(PROTECTED_BRANCHES_ENV))
    return defaults


def _set_protected_branches(branches: list[str], save: bool = True) -> None:
    global PROTECTED_BRANCHES, _PROTECTED_BRANCH_KEYS
    uniq = []
    seen = set()
    for b in (b.strip() for b in branches if b and b.strip()):
        key = b.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)
    PROTECTED_BRANCHES = uniq
    _PROTECTED_BRANCH_KEYS = {b.lower() for b in PROTECTED_BRANCHES}
    if save:
        state = _load_state()
        state["protected_branches"] = PROTECTED_BRANCHES
        _save_state(state)


def _normalize_engine_name(engine: object | None) -> str:
    if isinstance(engine, str):
        normalized = engine.strip().lower()
        if normalized == "codex":
            return "codex"
        if normalized == "kimi":
            return "kimi"
    return "claude"


def _engine_name_from_token(token: str) -> str:
    """Map a command token (cc/cx/openai/km/...) to a canonical engine name."""
    normalized = token.strip().lower()
    if normalized in ("codex", "cx", "openai"):
        return "codex"
    if normalized in ("kimi", "km"):
        return "kimi"
    return "claude"


def _normalize_model_name(value: object | None, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _normalize_reasoning_effort(value: object | None) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _global_runtime_config() -> dict[str, str | None]:
    return {
        "default_engine": _normalize_engine_name(DEFAULT_ENGINE),
        "claude_model": CLAUDE_MODEL,
        "codex_model": CODEX_MODEL,
        "kimi_model": KIMI_MODEL,
        "claude_reasoning_effort": CLAUDE_REASONING_EFFORT,
        "codex_reasoning_effort": CODEX_REASONING_EFFORT,
        "kimi_reasoning_effort": KIMI_REASONING_EFFORT,
    }


def _coerce_runtime_config(
    config: object | None,
    fallback: dict[str, str | None] | None = None,
) -> dict[str, str | None]:
    base = dict(fallback or _global_runtime_config())
    if not isinstance(config, dict):
        return base

    if "default_engine" in config:
        base["default_engine"] = _normalize_engine_name(config.get("default_engine"))

    base["claude_model"] = _normalize_model_name(
        config.get("claude_model"),
        str(base.get("claude_model") or CLAUDE_MODEL),
    )
    base["codex_model"] = _normalize_model_name(
        config.get("codex_model"),
        str(base.get("codex_model") or CODEX_MODEL),
    )
    base["kimi_model"] = _normalize_model_name(
        config.get("kimi_model"),
        str(base.get("kimi_model") or KIMI_MODEL),
    )

    if "claude_reasoning_effort" in config:
        base["claude_reasoning_effort"] = _normalize_reasoning_effort(
            config.get("claude_reasoning_effort")
        )
    if "codex_reasoning_effort" in config:
        base["codex_reasoning_effort"] = _normalize_reasoning_effort(
            config.get("codex_reasoning_effort")
        )
    if "kimi_reasoning_effort" in config:
        base["kimi_reasoning_effort"] = _normalize_reasoning_effort(
            config.get("kimi_reasoning_effort")
        )
    return base


def _apply_global_runtime_config(config: dict[str, str | None]) -> None:
    global DEFAULT_ENGINE, CLAUDE_MODEL, CODEX_MODEL, KIMI_MODEL
    global CLAUDE_REASONING_EFFORT, CODEX_REASONING_EFFORT, KIMI_REASONING_EFFORT
    DEFAULT_ENGINE = _normalize_engine_name(config.get("default_engine"))
    CLAUDE_MODEL = _normalize_model_name(config.get("claude_model"), CLAUDE_MODEL)
    CODEX_MODEL = _normalize_model_name(config.get("codex_model"), CODEX_MODEL)
    KIMI_MODEL = _normalize_model_name(config.get("kimi_model"), KIMI_MODEL)
    CLAUDE_REASONING_EFFORT = _normalize_reasoning_effort(config.get("claude_reasoning_effort"))
    CODEX_REASONING_EFFORT = _normalize_reasoning_effort(config.get("codex_reasoning_effort"))
    KIMI_REASONING_EFFORT = _normalize_reasoning_effort(config.get("kimi_reasoning_effort"))


def get_runtime_config(ch_id: int | None = None) -> dict[str, str | None]:
    global_config = _global_runtime_config()
    if ch_id is None:
        return dict(global_config)
    return _coerce_runtime_config(CHANNEL_RUNTIME_CONFIGS.get(ch_id), fallback=global_config)


def set_runtime_config(ch_id: int | None, config: dict[str, str | None]) -> dict[str, str | None]:
    global_config = _global_runtime_config()
    normalized = _coerce_runtime_config(config, fallback=global_config)
    if ch_id is None:
        _apply_global_runtime_config(normalized)
    else:
        if normalized == global_config:
            CHANNEL_RUNTIME_CONFIGS.pop(ch_id, None)
        else:
            CHANNEL_RUNTIME_CONFIGS[ch_id] = normalized
    _save_runtime_config()
    return get_runtime_config(ch_id)


def update_runtime_config(ch_id: int | None, **updates: object) -> dict[str, str | None]:
    config = get_runtime_config(ch_id)
    config.update(updates)
    return set_runtime_config(ch_id, config)


def runtime_scope_name(ch_id: int | None) -> str:
    return "global default" if ch_id is None else "this channel"


def _save_runtime_config() -> None:
    data = _load_state()
    global_config = _global_runtime_config()
    data["runtime_config"] = global_config
    channel_configs: dict[str, dict[str, str | None]] = {}
    for ch_id, config in CHANNEL_RUNTIME_CONFIGS.items():
        normalized = _coerce_runtime_config(config, fallback=global_config)
        if normalized != global_config:
            channel_configs[str(ch_id)] = normalized
    if channel_configs:
        data["channel_runtime_configs"] = channel_configs
    else:
        data.pop("channel_runtime_configs", None)
    _save_state(data)


def _load_runtime_config() -> None:
    global CHANNEL_RUNTIME_CONFIGS
    data = _load_state()
    global_config = _coerce_runtime_config(data.get("runtime_config"), fallback=_global_runtime_config())
    _apply_global_runtime_config(global_config)
    CHANNEL_RUNTIME_CONFIGS = {}
    scoped_configs = data.get("channel_runtime_configs")
    if not isinstance(scoped_configs, dict):
        return
    for ch_id, config in scoped_configs.items():
        try:
            ch_num = int(ch_id)
        except (TypeError, ValueError):
            continue
        CHANNEL_RUNTIME_CONFIGS[ch_num] = _coerce_runtime_config(config, fallback=global_config)


def _init_protected_branches() -> None:
    state = _load_state()
    branches = state.get("protected_branches") or _default_protected_branches()
    _set_protected_branches(branches, save=not bool(state.get("protected_branches")))


def is_protected_branch(branch: str) -> bool:
    return branch.strip().lower() in _PROTECTED_BRANCH_KEYS


_init_protected_branches()
_load_runtime_config()


def record_state(ch_id: int, cwd: str, branch: str | None = None) -> None:
    data = _load_state()
    channels = data.setdefault("channels", {})
    if not branch:
        try:
            branch = current_branch(cwd) or "?"
        except (FileNotFoundError, OSError):
            branch = "?"
    channels[str(ch_id)] = {
        "cwd": cwd,
        "repo": _canonical_repo(cwd),
        "branch": branch,
        "updated": int(time.time()),
    }
    data["last_active_channel"] = ch_id
    _save_state(data)


def restore_state() -> tuple[int | None, str | None, str | None, str | None]:
    data = _load_state()
    channels = data.get("channels", {}) or {}
    state_dirty = False
    # Prune stale worktrees for all known repos on startup
    pruned_repos: set[str] = set()
    for ch_id_str, info in channels.items():
        if isinstance(info, dict):
            # Fall back to canonical repo if the worktree path no longer exists
            repo = info.get("repo") or _canonical_repo(info.get("cwd", ""))
            cwd = info.get("cwd", "")
            if cwd and not pathlib.Path(cwd).exists():
                cwd = repo  # worktree gone, use canonical
                if repo and info.get("cwd") != repo:
                    info["cwd"] = repo
                    state_dirty = True
            if cwd:
                try:
                    channel_cwd[int(ch_id_str)] = cwd
                except ValueError:
                    continue
            if repo and repo not in pruned_repos and pathlib.Path(repo).exists():
                prune_worktrees(repo)
                pruned_repos.add(repo)
    last_id = data.get("last_active_channel")
    if last_id is None:
        if state_dirty:
            _save_state(data)
        return None, None, None, None
    info = channels.get(str(last_id))
    if not isinstance(info, dict):
        info = {}
    cwd = info.get("cwd")
    repo = info.get("repo") or _canonical_repo(cwd or "")
    branch = info.get("branch")
    # If cwd was a worktree that no longer exists, fall back to canonical
    if cwd and not pathlib.Path(cwd).exists():
        cwd = repo
        if repo and info.get("cwd") != repo:
            info["cwd"] = repo
            state_dirty = True
    if state_dirty:
        _save_state(data)
    checkout_error = None
    if cwd and branch and pathlib.Path(cwd).exists():
        res = run_git(["git", "checkout", branch], cwd)
        if res.returncode != 0:
            checkout_error = (res.stderr or res.stdout or "").strip() or "checkout failed"
    return last_id, cwd, branch, checkout_error


def _coerce_text(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _clean_codex_thread_id(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return ""
    return text


def _current_codex_thread_id(ch_id: int, *, include_saved: bool = True) -> str:
    session = active_sessions.get(ch_id)
    for source in (
        session,
        active_run_contexts.get(ch_id),
        channel_last_usage.get(ch_id),
    ):
        if isinstance(source, dict):
            thread_id = _clean_codex_thread_id(source.get("codex_thread_id"))
            if thread_id:
                return thread_id
    if include_saved:
        for source in (load_resume_context(ch_id), load_unfinished_task_snapshot(ch_id)):
            if isinstance(source, dict):
                thread_id = _clean_codex_thread_id(source.get("codex_thread_id"))
                if thread_id:
                    return thread_id
    return ""


def _tail_text(text: str, max_chars: int = CONTEXT_MAX_CHARS) -> str:
    if not text:
        return ""
    clean = strip_ansi(text)
    if len(clean) <= max_chars:
        return clean
    return clean[-max_chars:]


def save_queued_run_command(
    ch_id: int,
    command: str,
    images: list[str] | None = None,
    run_id: str | None = None,
) -> int:
    data = _load_state()
    queues = data.setdefault("queued_run_commands", {})
    key = str(ch_id)
    raw_queue = queues.get(key)
    if not isinstance(raw_queue, list):
        raw_queue = []
        queues[key] = raw_queue
    clean_images = [p.strip() for p in (images or []) if isinstance(p, str) and p.strip()]
    raw_queue.append({
        "ts": int(time.time()),
        "run_id": (run_id or "").strip(),
        "command": command.strip(),
        "images": clean_images,
    })
    _save_state(data)
    return len(raw_queue)


def queued_run_command_count(ch_id: int, run_id: str | None = None) -> int:
    data = _load_state()
    queues = data.get("queued_run_commands") or {}
    raw_queue = queues.get(str(ch_id))
    if not isinstance(raw_queue, list):
        return 0
    if run_id is None:
        return len(raw_queue)
    key = run_id.strip()
    return sum(
        1
        for item in raw_queue
        if isinstance(item, dict) and (str(item.get("run_id") or "").strip() == key)
    )


def pop_queued_run_commands(ch_id: int, run_id: str | None = None) -> list[dict]:
    data = _load_state()
    queues = data.get("queued_run_commands") or {}
    key = str(ch_id)
    raw_queue = queues.get(key)
    if not isinstance(raw_queue, list):
        return []

    selected: list[dict] = []
    remaining: list[dict] = []
    match_id = (run_id or "").strip() if run_id is not None else None
    for item in raw_queue:
        if not isinstance(item, dict):
            continue
        item_run_id = str(item.get("run_id") or "").strip()
        is_match = match_id is None or item_run_id == match_id or (match_id is not None and not item_run_id)
        clean_command = str(item.get("command") or "").strip()
        raw_images = item.get("images")
        clean_images = []
        if isinstance(raw_images, list):
            clean_images = [str(p).strip() for p in raw_images if isinstance(p, str) and str(p).strip()]
        if is_match:
            if clean_command or clean_images:
                selected.append({"command": clean_command, "images": clean_images})
        else:
            remaining.append({
                "run_id": item_run_id,
                "command": clean_command,
                "images": clean_images,
            })

    if remaining:
        queues[key] = remaining
        data["queued_run_commands"] = queues
    else:
        queues.pop(key, None)
        if queues:
            data["queued_run_commands"] = queues
        else:
            data.pop("queued_run_commands", None)
    _save_state(data)
    return selected


def build_queued_followup_task(entries: list[dict]) -> tuple[str, list[str]]:
    if not entries:
        return "Continue where you left off.", []

    lines = [
        "Additional user instructions were queued while your previous run was still in progress.",
        "Apply them now in order, preserving prior work unless a later instruction says otherwise.",
    ]
    images: list[str] = []
    for idx, entry in enumerate(entries, 1):
        cmd = str(entry.get("command") or "").strip()
        entry_images = [
            p for p in (entry.get("images") or [])
            if isinstance(p, str) and p.strip() and pathlib.Path(p).exists()
        ]
        images.extend(entry_images)
        if cmd:
            lines.append(f"{idx}. {cmd}")
        else:
            lines.append(f"{idx}. Inspect the queued image attachment(s) and apply the requested follow-up.")
    return "\n".join(lines), images


def save_resume_context(
    ch_id: int,
    cwd: str | None,
    engine: str,
    task: str,
    output: object | None,
    reason: str = "timeout",
) -> None:
    if not cwd:
        return
    data = _load_state()
    contexts = data.setdefault("resume_contexts", {})
    entry = {
        "ts": int(time.time()),
        "engine": engine,
        "cwd": cwd,
        "branch": current_branch(cwd) or "?",
        "task": task,
        "diff_stat": get_diff_stat(cwd),
        "output_tail": _tail_text(_coerce_text(output)),
        "reason": reason,
    }
    if _normalize_engine_name(engine) == "codex":
        codex_thread_id = _current_codex_thread_id(ch_id, include_saved=False)
        if codex_thread_id:
            entry["codex_thread_id"] = codex_thread_id
    contexts[str(ch_id)] = entry
    _save_state(data)


def load_resume_context(ch_id: int) -> dict | None:
    data = _load_state()
    contexts = data.get("resume_contexts") or {}
    entry = contexts.get(str(ch_id))
    if isinstance(entry, dict):
        return entry
    return None


def clear_resume_context(ch_id: int) -> bool:
    data = _load_state()
    contexts = data.get("resume_contexts") or {}
    if str(ch_id) in contexts:
        del contexts[str(ch_id)]
        data["resume_contexts"] = contexts
        _save_state(data)
        return True
    return False


def _head_commit_snapshot(path: str | None = None) -> dict[str, object]:
    if not path or not pathlib.Path(path).exists():
        return {}
    result = run_git(["git", "log", "-1", "--format=%H%n%s"], path)
    if result.returncode != 0:
        return {}
    lines = result.stdout.strip().splitlines()
    sha = lines[0].strip() if lines else ""
    subject = lines[1].strip() if len(lines) > 1 else ""
    entry: dict[str, object] = {}
    if sha:
        entry["sha"] = sha
    if subject:
        entry["subject"] = subject
    return entry


def save_unfinished_task_snapshot(
    ch_id: int,
    cwd: str | None,
    engine: str,
    task: str,
    output: object | None,
    runtime_config: dict[str, str | None] | None = None,
    reason: str = "timeout_exhausted",
    auto_commit: dict[str, object] | None = None,
) -> None:
    if not cwd:
        return
    path = pathlib.Path(cwd)
    if not path.exists():
        return

    session = active_sessions.get(ch_id)
    is_worktree = path.parent.name == ".worktrees"
    if not session and not is_worktree:
        return
    saved_runtime = _coerce_runtime_config(runtime_config, fallback=get_runtime_config(ch_id))
    official_task = _collapse_whitespace(
        str((session or {}).get("description") or task or "unfinished task")
    )
    followups = _clean_string_list((session or {}).get("followups"), limit=12)
    intent = _session_intent_summary(session, limit=600) or official_task
    turns = (session or {}).get("turns")
    if not isinstance(turns, int) or turns < 1:
        turns = 1

    entry: dict[str, object] = {
        "ts": int(time.time()),
        "reason": reason,
        "repo": _canonical_repo(cwd),
        "cwd": cwd,
        "branch": _safe_current_branch(cwd),
        "engine": engine,
        "model": get_model_for_engine(engine, runtime_config=saved_runtime, ch_id=ch_id),
        "runtime_config": dict(saved_runtime),
        "task": task.strip(),
        "official_task": official_task,
        "intent": intent,
        "turns": turns,
        "diff_stat": get_diff_stat(cwd),
        "output_tail": _tail_text(_coerce_text(output)),
    }
    if followups:
        entry["followups"] = followups
    totals = _coerce_usage_totals((session or {}).get("total_usage"))
    if totals:
        entry["total_usage"] = totals
    if _normalize_engine_name(engine) == "codex":
        codex_thread_id = _current_codex_thread_id(ch_id)
        if codex_thread_id:
            entry["codex_thread_id"] = codex_thread_id
    if auto_commit:
        commit_entry = {
            key: value
            for key, value in auto_commit.items()
            if value not in (None, "", [])
        }
        if commit_entry:
            entry["auto_commit"] = commit_entry

    data = _load_state()
    snapshots = data.setdefault("unfinished_tasks", {})
    snapshots[str(ch_id)] = entry
    _save_state(data)


def load_unfinished_task_snapshot(ch_id: int) -> dict | None:
    data = _load_state()
    snapshots = data.get("unfinished_tasks") or {}
    entry = snapshots.get(str(ch_id))
    if isinstance(entry, dict):
        return entry
    return None


def clear_unfinished_task_snapshot(ch_id: int) -> bool:
    data = _load_state()
    snapshots = data.get("unfinished_tasks") or {}
    key = str(ch_id)
    if key not in snapshots:
        return False
    del snapshots[key]
    if snapshots:
        data["unfinished_tasks"] = snapshots
    else:
        data.pop("unfinished_tasks", None)
    _save_state(data)
    return True


def _safe_current_branch(path: str | None) -> str:
    if not path or not pathlib.Path(path).exists():
        return "?"
    try:
        return current_branch(path) or "?"
    except Exception:
        return "?"


def _can_extend_plan_context(previous: dict | None, cwd: str | None, engine: str) -> bool:
    if not previous:
        return False
    prev_engine = (previous.get("engine") or "").strip()
    if prev_engine and prev_engine != engine:
        return False
    prev_cwd = (previous.get("cwd") or "").strip()
    if cwd and prev_cwd and prev_cwd != cwd:
        return False
    return True


def _append_plan_text(existing: str, addition: str) -> str:
    existing = existing.strip()
    addition = addition.strip()
    if not existing:
        return addition
    if not addition:
        return existing
    return f"{existing}\n\n---\n\n{addition}"


def save_plan_context(
    ch_id: int,
    cwd: str | None,
    engine: str,
    request: str,
    plan_output: object | None,
    runtime_config: dict[str, str | None] | None = None,
) -> None:
    data = _load_state()
    contexts = data.setdefault("plan_contexts", {})
    previous = contexts.get(str(ch_id))
    request_text = request.strip()
    plan_text = _tail_text(_coerce_text(plan_output), max_chars=PLAN_CONTEXT_MAX_CHARS)
    if _can_extend_plan_context(previous if isinstance(previous, dict) else None, cwd, engine):
        prev_request = (previous.get("request") or "").strip()
        prev_plan = (previous.get("plan") or "").strip()
        request_text = _tail_text(
            _append_plan_text(prev_request, request_text),
            max_chars=PLAN_CONTEXT_MAX_CHARS,
        )
        plan_text = _tail_text(
            _append_plan_text(prev_plan, plan_text),
            max_chars=PLAN_CONTEXT_MAX_CHARS,
        )
    entry = {
        "ts": int(time.time()),
        "engine": engine,
        "model": get_model_for_engine(engine, runtime_config=runtime_config, ch_id=ch_id),
        "cwd": cwd or "",
        "branch": _safe_current_branch(cwd),
        "request": request_text,
        "plan": plan_text,
    }
    contexts[str(ch_id)] = entry
    _save_state(data)


def load_plan_context(ch_id: int) -> dict | None:
    data = _load_state()
    contexts = data.get("plan_contexts") or {}
    entry = contexts.get(str(ch_id))
    if isinstance(entry, dict):
        return entry
    return None


def clear_plan_context(ch_id: int) -> bool:
    data = _load_state()
    contexts = data.get("plan_contexts") or {}
    if str(ch_id) in contexts:
        del contexts[str(ch_id)]
        data["plan_contexts"] = contexts
        _save_state(data)
        return True
    return False


def build_plan_prompt(
    request: str,
    ch_id: int,
    cwd: str | None,
    engine: str,
) -> str:
    previous = load_plan_context(ch_id)
    lines = [
        "Planning mode only.",
        "Do not edit files, write files, or make commits.",
        "Inspect the repository and produce an execution plan only.",
        "Return concise markdown with sections: Goal, Steps, Files, Validation, Risks.",
    ]
    if _can_extend_plan_context(previous, cwd, engine):
        prev_request = (previous.get("request") or "").strip()
        prev_plan = (previous.get("plan") or "").strip()
        lines.append("Existing saved plan context:")
        if prev_request:
            lines.append(f"Previous request: {prev_request}")
        if prev_plan:
            lines.append("Previous plan:")
            lines.append(prev_plan)
        lines.append("Extend and refine that plan using the new request below.")
    lines.append("Planning request:")
    lines.append(request.strip())
    return "\n".join(line for line in lines if line)


def build_do_prompt(plan_ctx: dict, request: str) -> str:
    saved_request = (plan_ctx.get("request") or "").strip()
    saved_plan = (plan_ctx.get("plan") or "").strip()
    saved_repo = (plan_ctx.get("cwd") or "").strip()
    saved_branch = (plan_ctx.get("branch") or "").strip()
    if saved_branch == "?":
        saved_branch = ""
    do_request = request.strip() or "Execute the saved plan now."
    lines = [
        "Execute the saved plan below in this repository.",
        "Do the implementation now; do not respond with only another plan.",
        "Run relevant checks for your changes and include a concise summary.",
    ]
    if saved_repo:
        lines.append(f"Saved planning repo: {saved_repo}")
    if saved_branch:
        lines.append(f"Saved planning branch: {saved_branch}")
    if saved_request:
        lines.append("Saved planning request:")
        lines.append(saved_request)
    if saved_plan:
        lines.append("Saved plan context:")
        lines.append(saved_plan)
    lines.append("Execution request:")
    lines.append(do_request)
    lines.append("If the code changed since planning, adapt while preserving intent.")
    return "\n".join(line for line in lines if line)


def build_resume_prompt(
    task: str,
    ch_id: int,
    cwd: str | None,
    engine: str,
) -> str:
    ctx = load_resume_context(ch_id)
    if not ctx:
        return task
    if cwd and ctx.get("cwd") and ctx.get("cwd") != cwd:
        return task
    if ctx.get("engine") and ctx.get("engine") != engine:
        return task
    if cwd:
        current = current_branch(cwd) or ""
        if ctx.get("branch") and current and ctx.get("branch") != current:
            return task

    snapshot = load_unfinished_task_snapshot(ch_id)
    if snapshot:
        snap_engine = _normalize_engine_name(snapshot.get("engine"))
        snap_branch = str(snapshot.get("branch") or "").strip()
        snap_repo = str(snapshot.get("repo") or _canonical_repo(str(snapshot.get("cwd") or ""))).strip()
        current_repo = _canonical_repo(cwd) if cwd else ""
        if snap_engine != engine:
            snapshot = None
        elif ctx.get("branch") and snap_branch and snap_branch != ctx.get("branch"):
            snapshot = None
        elif cwd and snap_repo and current_repo and snap_repo != current_repo:
            snapshot = None

    saved_diff = (ctx.get("diff_stat") or "").strip()
    current_diff = ""
    status_lines: list[str] = []
    status_known = False
    if cwd and pathlib.Path(cwd).exists():
        current_diff = get_diff_stat(cwd)
        status_lines = get_status_porcelain(cwd)
        status_known = True

    reason = str(ctx.get("reason") or "resume").strip()
    timeout_resume = reason.startswith("timeout")
    lines = [
        (
            "You are resuming after an interrupted run. The engine may have lost context."
            if timeout_resume
            else "You are continuing a prior run with saved context."
        ),
        "Use the saved context below to continue accurately.",
        f"Original task: {ctx.get('task', '').strip()}",
        f"Repo: {ctx.get('cwd', '').strip()}",
        f"Branch: {ctx.get('branch', '').strip()}",
        f"Saved diff: {saved_diff}" if saved_diff else "",
        f"Current diff now: {current_diff}" if current_diff else "",
    ]
    if snapshot:
        saved_official_task = str(snapshot.get("official_task") or "").strip()
        saved_intent = str(snapshot.get("intent") or "").strip()
        if saved_official_task:
            lines.append(f"Saved official task: {saved_official_task}")
        if saved_intent and saved_intent != saved_official_task:
            lines.append(f"Saved overall intent: {saved_intent}")
    if status_known:
        if status_lines:
            max_lines = 12
            lines.append("Current repo status (git status --porcelain):")
            lines.extend(status_lines[:max_lines])
            if len(status_lines) > max_lines:
                lines.append(f"... ({len(status_lines) - max_lines} more)")
        else:
            lines.append("Current repo status: clean (no changes detected)")
    output_tail = ctx.get("output_tail") or ""
    if output_tail.strip():
        lines.append("Last output (tail; may not reflect applied changes):")
        lines.append(output_tail.strip())
    lines.append("Important: prior output can claim changes that did not persist. "
                 "Treat the repo status above as the source of truth.")
    lines.append("Current request:")
    lines.append(task.strip())
    return "\n".join(line for line in lines if line)


def has_gh_cli() -> bool:
    try:
        return subprocess.run(
            ["gh", "--version"], capture_output=True, timeout=5
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_github_ssh() -> bool:
    """Test SSH connection to GitHub. Returns True if auth succeeds."""
    try:
        result = subprocess.run(
            ["ssh", "-T", "git@github.com"],
            capture_output=True, text=True, timeout=10,
        )
        # ssh -T returns exit code 1 on success with "Hi <user>!" in stderr
        return "successfully authenticated" in result.stderr.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_claude_cli() -> tuple[bool, str]:
    """Check if claude CLI is installed and has auth configured. Returns (ok, status)."""
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False, "not installed"
        version = (r.stdout or r.stderr).strip().splitlines()[0]
        if os.environ.get("ANTHROPIC_API_KEY"):
            return True, f"{version} (API key)"
        if (pathlib.Path.home() / ".claude" / ".credentials.json").exists():
            return True, f"{version} (OAuth)"
        return True, f"{version} (⚠️  no auth — run `claude login`)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, "not installed — run: npm install -g @anthropic-ai/claude-code"


def check_codex_cli() -> tuple[bool, str]:
    """Check if codex CLI is installed and has auth configured. Returns (ok, status)."""
    try:
        r = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False, "not installed"
        version = (r.stdout or r.stderr).strip().splitlines()[0]
        if os.environ.get("OPENAI_API_KEY"):
            return True, f"{version} (API key)"
        for cred in [
            pathlib.Path.home() / ".codex" / "auth.json",
            pathlib.Path.home() / ".config" / "codex" / "auth.json",
        ]:
            if cred.exists():
                return True, f"{version} (OAuth)"
        return True, f"{version} (⚠️  no auth — run `codex login`)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, "not installed — run: npm install -g @openai/codex"


def check_kimi_cli() -> tuple[bool, str]:
    """Check if kimi CLI is installed and has auth configured. Returns (ok, status)."""
    try:
        r = subprocess.run(["kimi", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False, "not installed"
        version = (r.stdout or r.stderr).strip().splitlines()[0]
        home = pathlib.Path(os.environ.get("KIMI_CODE_HOME") or (pathlib.Path.home() / ".kimi-code"))
        try:
            oauth_dir = home / "oauth"
            if oauth_dir.is_dir() and any(oauth_dir.iterdir()):
                return True, f"{version} (OAuth)"
            with open(home / "config.toml", "rb") as f:
                config = tomllib.load(f)
            providers = config.get("providers", {})
            if any(
                isinstance(p, dict) and str(p.get("api_key") or "").strip()
                for p in providers.values()
            ):
                return True, f"{version} (API key)"
        except Exception:
            pass
        return True, f"{version} (⚠️  no auth — run `kimi login`)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, "not installed — install Kimi Code CLI"


def _format_reset_at(ts: object) -> str | None:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Codex may return reset timestamps in Unix ms depending on transport/version.
    if value > 10_000_000_000:
        value /= 1000.0
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return None


def _extract_limit_line(text: str) -> str | None:
    """Return a concise limit-status line from CLI text output."""
    if not text:
        return None
    lines = [re.sub(r"\s+", " ", ln.strip()) for ln in strip_ansi(text).splitlines() if ln.strip()]
    if not lines:
        return None
    prefer = [
        ln for ln in lines
        if re.search(r"\b(remaining|left|reset|quota|limit|usage)\b", ln, re.IGNORECASE)
    ]
    selected = prefer[:2] if prefer else lines[:1]
    summary = " | ".join(selected)
    return summary[:240]


def get_claude_remaining_limit_summary() -> tuple[str | None, str | None]:
    """Best-effort: read Claude credentials and stats cache for usage info."""
    home = pathlib.Path.home()
    creds_path = home / ".claude" / ".credentials.json"
    stats_path = home / ".claude" / "stats-cache.json"

    parts: list[str] = []

    # Read subscription/tier info from credentials.
    if creds_path.is_file():
        try:
            with open(creds_path) as f:
                creds = json.load(f)
            oauth = creds.get("claudeAiOauth", {})
            plan = oauth.get("subscriptionType")
            tier = oauth.get("rateLimitTier", "")
            if plan:
                parts.append(f"{plan} plan")
            if tier:
                parts.append(f"tier {tier}")
        except (json.JSONDecodeError, OSError):
            pass

    # Read recent daily activity from stats cache.
    if stats_path.is_file():
        try:
            with open(stats_path) as f:
                stats = json.load(f)
            daily = stats.get("dailyActivity", [])
            if daily:
                today = time.strftime("%Y-%m-%d")
                today_entry = next((d for d in daily if d.get("date") == today), None)
                if today_entry:
                    parts.append(
                        f"today {today_entry.get('messageCount', 0)} msgs / "
                        f"{today_entry.get('toolCallCount', 0)} tools"
                    )
                else:
                    last = daily[-1]
                    parts.append(f"last active {last.get('date', '?')}")
        except (json.JSONDecodeError, OSError):
            pass

    if parts:
        return " · ".join(parts), None
    return None, "no Claude credentials or stats found"


def _format_codex_snapshot(label: str, snapshot: dict) -> str | None:
    windows: list[str] = []
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if not isinstance(used, (int, float)):
            continue
        remaining = max(0.0, 100.0 - float(used))
        part = f"{key} {remaining:.1f}% remaining"
        reset_at = _format_reset_at(window.get("resetsAt"))
        if reset_at:
            part += f" (resets {reset_at})"
        windows.append(part)

    credits = snapshot.get("credits")
    if isinstance(credits, dict):
        if credits.get("unlimited") is True:
            windows.append("credits unlimited")
        elif credits.get("hasCredits") and credits.get("balance"):
            windows.append(f"credits {credits.get('balance')}")

    if not windows:
        return None
    return f"{label}: " + " · ".join(windows)


def get_codex_remaining_limit_summary() -> tuple[str | None, str | None]:
    """Query Codex app-server for current account rate-limit usage."""
    init_req = {
        "id": "init",
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "PersonalAIBot", "version": "1.0"},
            "capabilities": None,
        },
    }
    limits_req = {
        "id": "limits",
        "method": "account/rateLimits/read",
    }
    try:
        proc = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return None, "Codex CLI not installed"

    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()
        # Wait for the initialize response before sending the next request.
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        init_lines: list[str] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready = sel.select(timeout=max(0.1, deadline - time.monotonic()))
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                init_lines.append(line)
                try:
                    msg = json.loads(line)
                    if msg.get("id") == "init":
                        break
                except json.JSONDecodeError:
                    pass
        sel.close()
        proc.stdin.write(json.dumps(limits_req) + "\n")
        proc.stdin.flush()
        # Wait for the rate-limits response.
        remaining_lines: list[str] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            remaining_lines.append(line)
            try:
                msg = json.loads(line)
                if msg.get("id") == "limits":
                    break
            except json.JSONDecodeError:
                pass
        proc.stdin.close()
        stdout = "".join(init_lines + remaining_lines)
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None, "timed out while checking Codex usage"
    except Exception:
        proc.kill()
        return None, "failed to query Codex usage limits"

    result_payload: dict | None = None
    error_message: str | None = None
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") != "limits":
            continue
        if isinstance(msg.get("result"), dict):
            result_payload = msg["result"]
        elif isinstance(msg.get("error"), dict):
            error_message = str(msg["error"].get("message") or "Codex rate-limit query failed")

    if not result_payload:
        if error_message:
            return None, error_message
        err_lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
        if err_lines:
            # Drop startup warnings and keep the most useful failure text.
            useful = [ln for ln in err_lines if "WARNING:" not in ln]
            tail = (useful or err_lines)[-1]
            return None, tail
        return None, "no response from Codex app-server"

    snapshots: list[tuple[str, dict]] = []
    by_id = result_payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for limit_id, snap in by_id.items():
            if isinstance(snap, dict):
                label = str(snap.get("limitName") or limit_id or "default")
                snapshots.append((label, snap))
    if not snapshots and isinstance(result_payload.get("rateLimits"), dict):
        snap = result_payload["rateLimits"]
        label = str(snap.get("limitName") or snap.get("limitId") or "default")
        snapshots.append((label, snap))

    if not snapshots:
        return None, "Codex returned no rate-limit windows"

    parts = [p for p in (_format_codex_snapshot(label, snap) for label, snap in snapshots) if p]
    if not parts:
        return None, "Codex returned no remaining-limit values"
    return " ; ".join(parts[:2]), None


def _normalize_path(path: str) -> str:
    p = pathlib.Path(path).expanduser()
    try:
        p = p.resolve()
    except Exception:
        p = p.absolute()
    return str(p)


def _is_claude_trusted(path: str) -> bool:
    """Return True if Claude Code can run non-interactively in this directory.
    In -p (print) mode, Claude Code v2.x does not require a per-project trust
    file — it only needs valid global auth credentials."""
    return (pathlib.Path.home() / ".claude" / ".credentials.json").exists()


def _load_codex_trusted_dirs() -> set[str]:
    """Return the set of directory paths that Codex has marked as trusted."""
    config_path = pathlib.Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return set()
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        return {
            _normalize_path(path)
            for path, settings in config.get("projects", {}).items()
            if isinstance(path, str) and settings.get("trust_level") == "trusted"
        }
    except Exception:
        return set()


def branch_merged_status(branch: str, path: str | None = None) -> tuple[bool, bool]:
    """Return (local_merged, remote_merged) for a branch."""
    local_merged = branch in [
        b.strip().lstrip("* ")
        for b in run_git(["git", "branch", "--merged"], path).stdout.strip().split("\n")
        if b.strip()
    ]
    remote_merged = f"origin/{branch}" in [
        b.strip()
        for b in run_git(["git", "branch", "-r", "--merged"], path).stdout.strip().split("\n")
        if b.strip()
    ]
    return local_merged, remote_merged


def parse_engine_and_task(content: str, default_engine: str) -> tuple[str, str]:
    lower = content.lower()
    for prefix in ("claude:", "cc:", "claude code:"):
        if lower.startswith(prefix):
            return "claude", content[len(prefix):].strip()
    for prefix in ("codex:", "cx:", "openai:"):
        if lower.startswith(prefix):
            return "codex", content[len(prefix):].strip()
    for prefix in ("kimi:", "km:"):
        if lower.startswith(prefix):
            return "kimi", content[len(prefix):].strip()
    return _normalize_engine_name(default_engine), content


def get_default_engine(ch_id: int | None = None) -> str:
    return _normalize_engine_name(get_runtime_config(ch_id).get("default_engine"))


def get_model_for_engine(
    engine: str,
    runtime_config: dict[str, str | None] | None = None,
    ch_id: int | None = None,
) -> str:
    config = _coerce_runtime_config(runtime_config, fallback=get_runtime_config(ch_id))
    engine_name = _normalize_engine_name(engine)
    key = {"codex": "codex_model", "kimi": "kimi_model"}.get(engine_name, "claude_model")
    return str(config[key])


def get_reasoning_for_engine(
    engine: str,
    runtime_config: dict[str, str | None] | None = None,
    ch_id: int | None = None,
) -> str | None:
    config = _coerce_runtime_config(runtime_config, fallback=get_runtime_config(ch_id))
    engine_name = _normalize_engine_name(engine)
    key = {"codex": "codex_reasoning_effort", "kimi": "kimi_reasoning_effort"}.get(
        engine_name, "claude_reasoning_effort"
    )
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def get_session_runtime_config(session: dict, ch_id: int) -> dict[str, str | None]:
    return _coerce_runtime_config(session.get("runtime_config"), fallback=get_runtime_config(ch_id))


def get_engine_label(engine: str) -> str:
    if engine == "codex":
        return "Codex CLI"
    if engine == "kimi":
        return "Kimi Code"
    return "Claude Code"


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMAGE_DIR = pathlib.Path("/tmp/botimages")


async def download_attachments(message: discord.Message) -> list[str]:
    """Download image attachments from a Discord message and return local paths."""
    paths: list[str] = []
    for att in message.attachments:
        ext = pathlib.Path(att.filename).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            continue
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        dest = IMAGE_DIR / f"{att.id}_{att.filename}"
        await att.save(dest)
        paths.append(str(dest))
    return paths


def _branch_exists(branch: str | None, path: str | None = None) -> bool:
    if not branch:
        return False
    return run_git(["git", "rev-parse", "--verify", branch], path).returncode == 0


def _remote_branch_exists(branch: str | None, path: str | None = None) -> bool:
    if not branch:
        return False
    return run_git(
        ["git", "show-ref", "--verify", f"refs/remotes/origin/{branch}"], path
    ).returncode == 0


def _origin_head_branch(path: str | None = None) -> str | None:
    result = run_git(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path
    )
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    if ref.startswith("origin/"):
        return ref.split("/", 1)[1] or None
    return None


def _is_feature_branch(name: str | None) -> bool:
    if not name or not BRANCH_PREFIX:
        return False
    return name.startswith(f"{BRANCH_PREFIX}/")


def _candidate_base_branches(path: str | None = None) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(name: str | None) -> None:
        if not name or name in seen:
            return
        seen.add(name)
        candidates.append(name)

    add(DEV_BRANCH)
    add(MAIN_BRANCH)
    add(_origin_head_branch(path))
    add("master")
    add("trunk")
    add("develop")
    cur = current_branch(path)
    if cur and cur != "HEAD":
        add(cur)
    for b in get_branch_list(path):
        add(b)
    return candidates


def _resolve_base_ref(path: str | None = None) -> str:
    """Return a base ref for diffs/compare (local if possible, else origin/...)."""
    candidates = _candidate_base_branches(path)
    for name in candidates:
        if _branch_exists(name, path) and not _is_feature_branch(name):
            return name
    for name in candidates:
        if _remote_branch_exists(name, path) and not _is_feature_branch(name):
            return f"origin/{name}"
    for name in candidates:
        if _branch_exists(name, path):
            return name
    for name in candidates:
        if _remote_branch_exists(name, path):
            return f"origin/{name}"
    return "HEAD"


def _ensure_local_branch(branch: str, path: str | None = None) -> bool:
    if _branch_exists(branch, path):
        return True
    if _remote_branch_exists(branch, path):
        result = run_git(["git", "branch", "--track", branch, f"origin/{branch}"], path)
        if result.returncode == 0:
            return True
        if "already exists" in (result.stderr or "").lower():
            return True
    return False


def _resolve_checkout_branch(path: str | None = None, avoid: str | None = None) -> str | None:
    """Return a local branch name suitable for checkout, creating tracking if needed."""
    candidates = _candidate_base_branches(path)
    for name in candidates:
        if avoid and name == avoid:
            continue
        if _is_feature_branch(name):
            continue
        if _ensure_local_branch(name, path):
            return name
    for name in candidates:
        if avoid and name == avoid:
            continue
        if _ensure_local_branch(name, path):
            return name
    return None


def _base_branch(path: str | None = None) -> str:
    """Return the base ref to diff against (dev/main if present, else default)."""
    return _resolve_base_ref(path)


def get_diff(path: str | None = None) -> str:
    base = _base_branch(path)
    # Committed changes on this branch vs base
    committed = run_git(["git", "diff", f"{base}...HEAD"], path).stdout or ""
    # Plus any uncommitted changes not yet auto-committed
    uncommitted = run_git(["git", "diff"], path).stdout or ""
    staged = run_git(["git", "diff", "--cached"], path).stdout or ""
    untracked = run_git(["git", "ls-files", "--others", "--exclude-standard"], path).stdout.strip()
    combined = committed + uncommitted + staged
    if untracked:
        combined += f"\n\nNew files:\n{untracked}"
    return combined.strip() or "(no changes detected)"


def _collect_unified_review_sections(path: str | None = None) -> list[tuple[str, str]]:
    base = _base_branch(path)
    sections: list[tuple[str, str]] = []

    committed = run_git(
        ["git", "diff", "--unified=0", "--find-renames", "--no-color", f"{base}...HEAD"],
        path,
    ).stdout.strip()
    if committed:
        sections.append((f"committed vs `{base}`", committed))

    staged = run_git(
        ["git", "diff", "--cached", "--unified=0", "--find-renames", "--no-color"],
        path,
    ).stdout.strip()
    if staged:
        sections.append(("staged vs HEAD", staged))

    unstaged = run_git(
        ["git", "diff", "--unified=0", "--find-renames", "--no-color"],
        path,
    ).stdout.strip()
    if unstaged:
        sections.append(("unstaged vs index", unstaged))

    untracked = run_git(
        ["git", "ls-files", "--others", "--exclude-standard"],
        path,
    ).stdout.strip()
    if untracked:
        patches: list[str] = []
        for rel_path in (line.strip() for line in untracked.splitlines() if line.strip()):
            patch = run_git(
                ["git", "diff", "--no-index", "--unified=0", "--no-color", "/dev/null", rel_path],
                path,
            ).stdout.strip()
            if patch:
                patches.append(patch)
                continue
            patches.append(
                "\n".join(
                    [
                        f"diff --git a/{rel_path} b/{rel_path}",
                        "new file mode 100644",
                        "--- /dev/null",
                        f"+++ b/{rel_path}",
                        "@@ -0,0 +1 @@",
                        f"+(new untracked file: {rel_path})",
                    ]
                )
            )
        if patches:
            sections.append(("untracked files", "\n\n".join(patches)))

    return sections


def _strip_diff_path(path_token: str, prefix: str) -> str:
    token = path_token.strip().strip('"')
    if token.startswith(prefix):
        return token[len(prefix):]
    return token


def _parse_diff_header_paths(line: str) -> tuple[str, str]:
    parts = line.split(" ", 3)
    if len(parts) < 4:
        return "", ""
    before = _strip_diff_path(parts[2], "a/")
    after = _strip_diff_path(parts[3], "b/")
    return before, after


def _review_file_label(file_state: dict) -> str:
    rename_from = str(file_state.get("rename_from") or "").strip()
    rename_to = str(file_state.get("rename_to") or "").strip()
    if rename_from and rename_to and rename_from != rename_to:
        return f"{rename_from} -> {rename_to}"
    after = str(file_state.get("after") or "").strip()
    if after and after != "/dev/null":
        return after
    before = str(file_state.get("before") or "").strip()
    if before and before != "/dev/null":
        return before
    return "(unknown file)"


def _parse_unified_review_units(diff_text: str, source_label: str) -> list[dict]:
    units: list[dict] = []
    current_file: dict | None = None
    current_hunk: dict | None = None

    def flush_hunk() -> None:
        nonlocal current_hunk
        if current_file is None or current_hunk is None:
            current_hunk = None
            return
        before_lines = current_hunk.get("before") or []
        after_lines = current_hunk.get("after") or []
        if before_lines or after_lines:
            current_file.setdefault("hunks", []).append(current_hunk)
        current_hunk = None

    def flush_file() -> None:
        nonlocal current_file
        flush_hunk()
        if current_file is None:
            return

        file_label = _review_file_label(current_file)
        rename_from = str(current_file.get("rename_from") or "").strip()
        rename_to = str(current_file.get("rename_to") or "").strip()
        has_rename = bool(rename_from and rename_to and rename_from != rename_to)
        before_path = str(current_file.get("before") or "").strip()
        after_path = str(current_file.get("after") or "").strip()
        old_mode = str(current_file.get("old_mode") or "").strip()
        new_mode = str(current_file.get("new_mode") or "").strip()

        if has_rename:
            units.append(
                {
                    "file": file_label,
                    "before": rename_from,
                    "after": rename_to,
                    "source": source_label,
                    "kind": "rename",
                    "before_path": before_path or rename_from,
                    "after_path": after_path or rename_to,
                    "hunk_context": "",
                }
            )

        if old_mode and new_mode and old_mode != new_mode:
            units.append(
                {
                    "file": file_label,
                    "before": f"mode {old_mode}",
                    "after": f"mode {new_mode}",
                    "source": source_label,
                    "kind": "metadata",
                    "before_path": before_path,
                    "after_path": after_path,
                    "hunk_context": "",
                }
            )

        if current_file.get("binary"):
            units.append(
                {
                    "file": file_label,
                    "before": "(binary content before)",
                    "after": "(binary content after)",
                    "source": source_label,
                    "kind": "binary",
                    "before_path": before_path,
                    "after_path": after_path,
                    "hunk_context": "",
                }
            )

        hunks = current_file.get("hunks") or []
        for hunk in hunks:
            units.append(
                {
                    "file": file_label,
                    "before": "\n".join(hunk.get("before") or []),
                    "after": "\n".join(hunk.get("after") or []),
                    "source": source_label,
                    "kind": "hunk",
                    "before_path": before_path,
                    "after_path": after_path,
                    "hunk_context": str(hunk.get("context") or "").strip(),
                }
            )

        if not has_rename and not hunks and not current_file.get("binary") and not (old_mode and new_mode):
            if current_file.get("new_file"):
                units.append(
                    {
                        "file": file_label,
                        "before": "(file did not exist)",
                        "after": "(new file added)",
                        "source": source_label,
                        "kind": "new_file",
                        "before_path": before_path,
                        "after_path": after_path,
                        "hunk_context": "",
                    }
                )
                current_file = None
                return
            if current_file.get("deleted_file"):
                units.append(
                    {
                        "file": file_label,
                        "before": "(file removed)",
                        "after": "(none)",
                        "source": source_label,
                        "kind": "deleted_file",
                        "before_path": before_path,
                        "after_path": after_path,
                        "hunk_context": "",
                    }
                )
                current_file = None
                return
            units.append(
                {
                    "file": file_label,
                    "before": "(no textual before lines captured)",
                    "after": "(no textual after lines captured)",
                    "source": source_label,
                    "kind": "metadata",
                    "before_path": before_path,
                    "after_path": after_path,
                    "hunk_context": "",
                }
            )

        current_file = None

    for raw_line in diff_text.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("diff --git "):
            flush_file()
            before, after = _parse_diff_header_paths(line)
            current_file = {
                "before": before,
                "after": after,
                "rename_from": "",
                "rename_to": "",
                "old_mode": "",
                "new_mode": "",
                "new_file": False,
                "deleted_file": False,
                "binary": False,
                "hunks": [],
            }
            continue

        if current_file is None:
            continue

        if line.startswith("rename from "):
            current_file["rename_from"] = line[len("rename from "):].strip()
            continue
        if line.startswith("rename to "):
            current_file["rename_to"] = line[len("rename to "):].strip()
            continue
        if line.startswith("old mode "):
            current_file["old_mode"] = line[len("old mode "):].strip()
            continue
        if line.startswith("new mode "):
            current_file["new_mode"] = line[len("new mode "):].strip()
            continue
        if line.startswith("new file mode "):
            current_file["new_file"] = True
            continue
        if line.startswith("deleted file mode "):
            current_file["deleted_file"] = True
            continue
        if line.startswith("Binary files ") or line == "GIT binary patch":
            current_file["binary"] = True
            continue
        if line.startswith("@@ "):
            flush_hunk()
            context_match = _HUNK_CONTEXT_RE.match(line)
            context = context_match.group(1).strip() if context_match else ""
            current_hunk = {"context": context, "before": [], "after": []}
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if current_hunk is None:
            continue
        if line.startswith("-") and not line.startswith("---"):
            current_hunk["before"].append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            current_hunk["after"].append(line[1:])

    flush_file()
    return units


def _reason_for_review_unit(unit: dict, intent_summary: str) -> str:
    file_label = str(unit.get("file") or "(unknown file)")
    source_label = str(unit.get("source") or "current changes")
    before_text = str(unit.get("before") or "")
    after_text = str(unit.get("after") or "")
    before_lines = len(before_text.splitlines()) if before_text else 0
    after_lines = len(after_text.splitlines()) if after_text else 0
    hunk_context = str(unit.get("hunk_context") or "").strip()
    context_text = f" around `{hunk_context}`" if hunk_context else ""
    intent_text = (
        f"to support the request: {intent_summary}"
        if intent_summary
        else "to match the generated session changes"
    )
    kind = str(unit.get("kind") or "hunk")

    if kind == "rename":
        before_path = str(unit.get("before") or "").strip() or str(unit.get("before_path") or "").strip()
        after_path = str(unit.get("after") or "").strip() or str(unit.get("after_path") or "").strip()
        return f"Renamed `{before_path}` to `{after_path}` ({source_label}) {intent_text}."
    if kind == "binary":
        return f"Updated binary content in `{file_label}` ({source_label}) {intent_text}."
    if kind == "new_file":
        return f"Added new file `{file_label}` ({source_label}) {intent_text}."
    if kind == "deleted_file":
        return f"Removed file `{file_label}` ({source_label}) {intent_text}."
    if kind == "metadata":
        return f"Adjusted file metadata for `{file_label}` ({source_label}) {intent_text}."
    if before_lines and after_lines:
        return (
            f"Replaced {before_lines} line(s) with {after_lines} line(s) in `{file_label}`"
            f"{context_text} ({source_label}) {intent_text}."
        )
    if after_lines:
        return (
            f"Added {after_lines} line(s) in `{file_label}`{context_text} "
            f"({source_label}) {intent_text}."
        )
    if before_lines:
        return (
            f"Removed {before_lines} line(s) from `{file_label}`{context_text} "
            f"({source_label}) {intent_text}."
        )
    return f"Updated `{file_label}` ({source_label}) {intent_text}."


def _collect_review_units(path: str | None = None) -> list[dict]:
    review_units: list[dict] = []
    for source_label, diff_text in _collect_unified_review_sections(path):
        review_units.extend(_parse_unified_review_units(diff_text, source_label))
    return review_units


def build_major_change_review(path: str | None = None, session: dict | None = None) -> list[dict]:
    review_units = _collect_review_units(path)
    intent_summary = _session_intent_summary(session)
    entries: list[dict] = []
    for unit in review_units:
        before_text = str(unit.get("before") or "").rstrip("\n")
        after_text = str(unit.get("after") or "").rstrip("\n")
        entries.append(
            {
                "file": str(unit.get("file") or "(unknown file)"),
                "source": str(unit.get("source") or "current changes"),
                "before": before_text if before_text else "(none)",
                "after": after_text if after_text else "(none)",
                "why": _reason_for_review_unit(unit, intent_summary),
            }
        )
    return entries


def _truncate_inline_text(text: str, limit: int = 80) -> str:
    clean = _collapse_whitespace(text)
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


def _summary_file_path(file_label: str) -> str:
    if " -> " in file_label:
        return file_label.split(" -> ", 1)[1].strip() or file_label
    return file_label


def _summary_file_kind(file_label: str) -> str:
    ext = pathlib.Path(_summary_file_path(file_label)).suffix.lower()
    if ext == ".py":
        return "Python file"
    if ext in {".md", ".rst"}:
        return "documentation file"
    if ext == ".sh":
        return "shell script"
    if ext == ".json":
        return "JSON file"
    if ext == ".toml":
        return "TOML file"
    if ext in {".yml", ".yaml"}:
        return "YAML file"
    if ext == ".txt":
        return "text file"
    return "file"


def _summary_is_doc_file(file_label: str) -> bool:
    return pathlib.Path(_summary_file_path(file_label)).suffix.lower() in {".md", ".rst", ".txt"}


def _summary_looks_like_command(token: str) -> bool:
    clean = token.strip().lower()
    if not clean or len(clean) > 40:
        return False
    if "/" in clean or "." in clean:
        return False
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9:_ -]*", clean))


def _summary_command_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"render\((?:'|\")([^'\"]{1,20})(?:'|\")\)", text):
        clean = _collapse_whitespace(raw)
        if clean and _summary_looks_like_command(clean) and clean not in tokens:
            tokens.append(clean)
    for raw in re.findall(r"`([^`]{1,40})`", text):
        clean = _collapse_whitespace(raw)
        if clean and _summary_looks_like_command(clean) and clean not in tokens:
            tokens.append(clean)
    return tokens[:4]


def _summary_join_inline_codes(tokens: list[str]) -> str:
    rendered = [f"`{_truncate_inline_text(token, 30)}`" for token in tokens if token]
    if not rendered:
        return ""
    if len(rendered) == 1:
        return rendered[0]
    if len(rendered) == 2:
        return f"{rendered[0]} and {rendered[1]}"
    return ", ".join(rendered[:-1]) + f", and {rendered[-1]}"


def _summary_join_phrases(parts: list[str]) -> str:
    clean = [part for part in parts if part]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return ", ".join(clean[:-1]) + f", and {clean[-1]}"


def _summary_definition_targets(text: str) -> list[tuple[str, str]]:
    definitions: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in str(text or "").splitlines():
        if line[:1].isspace():
            continue
        clean = _collapse_whitespace(line)
        item: tuple[str, str] | None = None
        match = re.match(r"^(?:async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", clean)
        if match:
            item = ("function", match.group(1))
        else:
            match = re.match(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\b", clean)
            if match:
                item = ("class", match.group(1))
        if item and item not in seen:
            seen.add(item)
            definitions.append(item)
    return definitions[:4]


def _summary_group_definitions(definitions: list[tuple[str, str]]) -> str | None:
    if not definitions:
        return None
    kinds = {kind for kind, _ in definitions}
    names = [f"`{_truncate_inline_text(name, 40)}`" for _, name in definitions]
    if len(kinds) == 1:
        kind = next(iter(kinds))
        noun = kind if len(names) == 1 else f"{kind}s"
        return f"the {_summary_join_phrases(names)} {noun}"
    return f"the definitions for {_summary_join_phrases(names)}"


def _summary_focus_from_line(line: str, file_label: str) -> str | None:
    clean = _collapse_whitespace(line).strip()
    if not clean:
        return None

    trivial = {
        "(",
        ")",
        "[",
        "]",
        "{",
        "}",
        ",",
        ":",
        "return",
        "pass",
        "continue",
        "break",
        "else:",
        "try:",
        "finally:",
    }
    if clean in trivial or re.fullmatch(r"[\[\]{}(),.:]+", clean):
        return None

    command_tokens = _summary_command_tokens(clean)
    if command_tokens:
        rendered_tokens = _summary_join_inline_codes(command_tokens)
        lowered = clean.lower()
        looks_like_guidance = (
            _summary_is_doc_file(file_label)
            or "send(" in lowered
            or "reply" in lowered
            or "prompt" in lowered
            or "follow-up" in lowered
            or "follow up" in lowered
            or "continue" in lowered
            or "finished" in lowered
            or "when done" in lowered
        )
        if looks_like_guidance:
            if _summary_is_doc_file(file_label):
                return f"documentation for {rendered_tokens}"
            return f"guidance mentioning {rendered_tokens}"

    if not clean.startswith(("def ", "async def ", "class ")):
        call_names = [
            name
            for name in re.findall(r"([A-Za-z_][A-Za-z0-9_\.]*)\s*\(", clean)
            if name not in {"if", "for", "while", "return"}
        ]
        if call_names:
            preferred = next(
                (name for name in reversed(call_names) if name not in {"ch.send", "channel.send"}),
                call_names[-1],
            )
            return f"a call to `{_truncate_inline_text(preferred, 40)}`"

    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\s*=", clean)
    if match:
        return f"the `{_truncate_inline_text(match.group(1), 40)}` assignment"

    return None


def _summary_focus_from_text(text: str, file_label: str) -> str | None:
    for line in str(text or "").splitlines():
        focus = _summary_focus_from_line(line, file_label)
        if focus:
            return focus
    return None


def _summary_target_from_text(text: str, file_label: str) -> str | None:
    clean = _collapse_whitespace(text)
    if not clean:
        return None

    match = re.search(r'lower\s*==\s*["\']([^"\']{1,30})["\']', clean)
    if match:
        return f"the `{match.group(1).strip()}` command handling"

    match = re.match(r"^(?:async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", clean)
    if match:
        return f"the `{match.group(1)}` function"

    match = re.match(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\b", clean)
    if match:
        return f"the `{match.group(1)}` class"

    match = re.match(r"^#{1,6}\s+(.+?)\s*$", clean)
    if match:
        return f"the `{_truncate_inline_text(match.group(1), 60)}` section"

    match = re.match(r"([A-Z][A-Z0-9_]{2,})\s*=", clean)
    if match:
        return f"the `{match.group(1)}` setting"

    inline_codes = [_collapse_whitespace(token) for token in re.findall(r"`([^`]{1,60})`", clean)]
    for token in inline_codes:
        if _summary_looks_like_command(token):
            suffix = " docs" if _summary_is_doc_file(file_label) else ""
            return f"the `{token}` command{suffix}"
    if inline_codes:
        return f"the `{_truncate_inline_text(inline_codes[0], 50)}` reference"

    if _summary_is_doc_file(file_label):
        stripped = clean.strip(" |-")
        if stripped and len(stripped) <= 70:
            return f"the `{_truncate_inline_text(stripped, 50)}` section"

    return None


def _summary_target_for_unit(unit: dict) -> str | None:
    file_label = str(unit.get("file") or "(unknown file)")
    changed_lines: list[str] = []
    changed_lines.extend(str(unit.get("after") or "").splitlines())
    changed_lines.extend(str(unit.get("before") or "").splitlines())

    for line in changed_lines:
        target = _summary_target_from_text(line, file_label)
        if target and target.endswith(("command handling", " function", " class", " setting", " section")):
            return target

    hunk_context = str(unit.get("hunk_context") or "")
    target = _summary_target_from_text(hunk_context, file_label)
    if target:
        return target

    for line in changed_lines:
        target = _summary_target_from_text(line, file_label)
        if target:
            return target
    return None


def _summary_clause_for_unit(unit: dict) -> str | None:
    kind = str(unit.get("kind") or "hunk")
    file_label = str(unit.get("file") or "(unknown file)")

    if kind == "rename":
        before_path = _truncate_inline_text(str(unit.get("before") or "").strip() or str(unit.get("before_path") or "").strip(), 60)
        after_path = _truncate_inline_text(str(unit.get("after") or "").strip() or str(unit.get("after_path") or "").strip(), 60)
        if before_path and after_path and before_path != after_path:
            return f"renamed this file from `{before_path}` to `{after_path}`"
        return "renamed this file"

    if kind == "binary":
        return "updated the binary contents"

    if kind == "new_file":
        return "added this new file"

    if kind == "deleted_file":
        return "removed this file"

    if kind == "metadata":
        before_text = str(unit.get("before") or "").strip()
        after_text = str(unit.get("after") or "").strip()
        if before_text.startswith("mode ") and after_text.startswith("mode "):
            return f"changed the file mode from `{before_text[5:]}` to `{after_text[5:]}`"
        return "changed file metadata"

    target = _summary_target_for_unit(unit)
    before_text = str(unit.get("before") or "").strip()
    after_text = str(unit.get("after") or "").strip()
    if before_text and after_text:
        verb = "updated"
    elif after_text:
        verb = "added"
    else:
        verb = "removed"

    before_definitions = _summary_definition_targets(before_text)
    after_definitions = _summary_definition_targets(after_text)
    added_definitions = [item for item in after_definitions if item not in before_definitions]
    removed_definitions = [item for item in before_definitions if item not in after_definitions]
    if verb == "added" and added_definitions:
        grouped = _summary_group_definitions(added_definitions)
        if grouped:
            command_tokens = _summary_command_tokens(after_text)
            if command_tokens:
                return f"added {grouped} covering {_summary_join_inline_codes(command_tokens)}"
            return f"added {grouped}"
    if verb == "removed" and removed_definitions:
        grouped = _summary_group_definitions(removed_definitions)
        if grouped:
            return f"removed {grouped}"

    before_focus = _summary_focus_from_text(before_text, file_label)
    after_focus = _summary_focus_from_text(after_text, file_label)

    if (
        target
        and verb == "updated"
        and before_focus
        and after_focus
        and before_focus != after_focus
        and before_focus.startswith(("guidance ", "documentation "))
        and after_focus.startswith(("guidance ", "documentation "))
    ):
        return f"changed {target} from {before_focus} to {after_focus}"

    if (
        target
        and verb == "updated"
        and before_focus
        and after_focus
        and before_focus.startswith(("guidance ", "documentation "))
        and after_focus.startswith("a call to ")
    ):
        label = "guidance" if before_focus.startswith("guidance ") else "documentation"
        return f"replaced inline {label} with {after_focus} in {target}"

    focus = before_focus if verb == "removed" else after_focus or before_focus
    if focus:
        action = "changed" if verb == "updated" else verb
        if target and focus != target:
            prep = "from" if verb == "removed" else "in"
            return f"{action} {focus} {prep} {target}"
        return f"{action} {focus}"

    if target:
        return f"{verb} {target}"

    if verb == "added":
        return f"added content to this {_summary_file_kind(file_label)}"
    if verb == "removed":
        return f"removed content from this {_summary_file_kind(file_label)}"
    return f"updated part of this {_summary_file_kind(file_label)}"


def _build_file_change_description(item: dict) -> str:
    file_label = str(item["file"])
    rename_from = str(item.get("rename_from") or "").strip()
    rename_to = str(item.get("rename_to") or "").strip()
    renamed = bool(rename_from and rename_to and rename_from != rename_to)
    new_file = bool(item.get("new_file") and not item.get("deleted_file"))
    deleted_file = bool(item.get("deleted_file") and not item.get("new_file"))
    binary = bool(item.get("binary"))
    metadata_only = bool(item.get("metadata_only") and not item.get("hunks") and not binary)
    added = int(item.get("added") or 0)
    removed = int(item.get("removed") or 0)
    hunks = int(item.get("hunks") or 0)
    file_kind = _summary_file_kind(file_label)

    if renamed:
        overview = (
            "Renamed this "
            f"{file_kind} from `{_truncate_inline_text(rename_from, 60)}` "
            f"to `{_truncate_inline_text(rename_to, 60)}`."
        )
    elif new_file:
        overview = f"Added this {file_kind}."
    elif deleted_file:
        overview = f"Removed this {file_kind}."
    elif binary and not hunks:
        overview = f"Updated this {file_kind} with binary changes."
    elif metadata_only:
        overview = f"Adjusted metadata for this {file_kind}."
    else:
        overview = f"Updated this {file_kind}."

    clauses = _clean_string_list(item.get("clauses"))
    redundant = {
        "renamed this file",
        "added this new file",
        "removed this file",
        "updated the binary contents",
        "changed file metadata",
    }
    clauses = [clause for clause in clauses if clause not in redundant]
    if renamed:
        clauses = [clause for clause in clauses if not clause.startswith("renamed this file")]

    generic_prefixes = (
        "updated part of this ",
        "added content to this ",
        "removed content from this ",
    )
    specific_clauses = [clause for clause in clauses if not clause.startswith(generic_prefixes)]
    if specific_clauses:
        clauses = specific_clauses

    if clauses:
        indexed_clauses = list(enumerate(clauses))
        indexed_clauses.sort(key=lambda pair: (_summary_clause_priority(pair[1]), pair[0]))
        selected = [clause for _, clause in indexed_clauses[:3]]
        if len(selected) == 1:
            detail = selected[0]
        else:
            detail = "; ".join(selected[:-1]) + f"; and {selected[-1]}"
        return f"{overview} Notable changes: {detail}."

    fallback_bits: list[str] = []
    if added or removed:
        if added and removed:
            fallback_bits.append(f"+{added}/-{removed}")
        elif added:
            fallback_bits.append(f"+{added}")
        else:
            fallback_bits.append(f"-{removed}")
    if hunks and not binary:
        area_label = "edit area" if hunks == 1 else "edit areas"
        fallback_bits.append(f"{hunks} {area_label}")
    if fallback_bits:
        return f"{overview[:-1]} ({', '.join(fallback_bits)})."
    return overview


def _summary_clause_priority(clause: str) -> int:
    lower = clause.lower()
    if "guidance " in lower or "documentation " in lower or "covering `" in clause:
        return 0
    if "command handling" in lower:
        return 1
    if " function" in lower or " class" in lower or "definitions for " in lower:
        return 2
    if " section" in lower or " setting" in lower:
        return 3
    if "call to " in lower:
        return 4
    if " assignment" in lower:
        return 5
    return 6


def build_change_summary_lines(path: str | None = None, session: dict | None = None) -> list[str]:
    review_units = _collect_review_units(path)
    if not review_units:
        return []

    file_summaries: dict[str, dict] = {}

    for unit in review_units:
        file_label = str(unit.get("file") or "(unknown file)")
        before_path = str(unit.get("before_path") or "").strip()
        after_path = str(unit.get("after_path") or "").strip()
        kind = str(unit.get("kind") or "hunk")
        summary = file_summaries.setdefault(
            file_label,
            {
                "file": file_label,
                "rename_from": "",
                "rename_to": "",
                "new_file": False,
                "deleted_file": False,
                "binary": False,
                "metadata_only": False,
                "hunks": 0,
                "added": 0,
                "removed": 0,
                "clauses": [],
            },
        )

        if before_path == "/dev/null":
            summary["new_file"] = True
        if after_path == "/dev/null":
            summary["deleted_file"] = True

        clause = _summary_clause_for_unit(unit)
        if clause:
            clauses = summary["clauses"]
            if clause not in clauses:
                clauses.append(clause)

        if kind == "rename":
            rename_from = str(unit.get("before") or "").strip() or before_path
            rename_to = str(unit.get("after") or "").strip() or after_path
            summary["rename_from"] = rename_from
            summary["rename_to"] = rename_to
            continue

        if kind == "binary":
            summary["binary"] = True
            continue

        if kind == "metadata":
            summary["metadata_only"] = True
            continue

        if kind != "hunk":
            continue

        before_lines = len(str(unit.get("before") or "").splitlines())
        after_lines = len(str(unit.get("after") or "").splitlines())
        summary["metadata_only"] = False
        summary["hunks"] += 1
        summary["added"] += after_lines
        summary["removed"] += before_lines

    summaries = sorted(file_summaries.values(), key=lambda item: str(item["file"]).lower())
    total_added = sum(int(item["added"]) for item in summaries)
    total_removed = sum(int(item["removed"]) for item in summaries)
    added_files = sum(1 for item in summaries if item["new_file"] and not item["deleted_file"])
    removed_files = sum(1 for item in summaries if item["deleted_file"] and not item["new_file"])
    renamed_files = sum(
        1
        for item in summaries
        if item["rename_from"] and item["rename_to"] and item["rename_from"] != item["rename_to"]
    )

    headline_bits = [f"{len(summaries)} file(s) changed"]
    if total_added or total_removed:
        headline_bits.append(f"+{total_added}/-{total_removed}")
    if added_files:
        headline_bits.append(f"{added_files} added")
    if removed_files:
        headline_bits.append(f"{removed_files} removed")
    if renamed_files:
        headline_bits.append(f"{renamed_files} renamed")

    lines = [f"Summary: {', '.join(headline_bits)}"]
    intent_summary = _session_intent_summary(session)
    if intent_summary:
        lines.append(f"Request: {intent_summary}")
    lines.append("")

    for item in summaries:
        file_label = str(item["file"])
        lines.append(f"- `{file_label}`: {_build_file_change_description(item)}")

    return lines


def _review_action_prompt(cwd: str | None = None, *, bold: bool = False) -> str:
    repo_path = cwd or REPO_PATH
    dev_exists = run_git(["git", "rev-parse", "--verify", DEV_BRANCH], repo_path).returncode == 0

    def render(token: str) -> str:
        return f"**{token}**" if bold else f"`{token}`"

    merge_hint = f"merge to `{DEV_BRANCH}`" if dev_exists else "choose a merge target"
    return (
        f"{render('yes')} to commit, push & {merge_hint}, "
        f"{render('skip')} to push without merging, or {render('no')} to discard"
    )


def _working_session_command_help(cwd: str | None = None) -> str:
    repo_path = cwd or REPO_PATH
    dev_exists = run_git(["git", "rev-parse", "--verify", DEV_BRANCH], repo_path).returncode == 0
    merge_hint = f"merge to `{DEV_BRANCH}`" if dev_exists else "choose a merge target"
    return "\n".join(
        [
            "**Next steps**",
            "- `done`: show the current change summary again and switch to approval mode",
            f"- after `done`, `yes`: commit, push, and {merge_hint}",
            "- after `done`, `skip`: commit and push without merging",
            "- after `done`, `no`: discard this session's changes",
            "- still iterating: send a follow-up, `diff`, or `review`",
        ]
    )


def _working_session_guidance(cwd: str | None = None) -> str:
    return (
        "If the task is done, send `done` to review the exact change summary and enter approval mode, "
        f"then reply {_review_action_prompt(cwd)}. "
        "Otherwise send a follow-up to keep iterating, `diff` for a quick peek, or `review` for major changes."
    )


def _format_review_entry_parts(entry: dict, index: int, total: int) -> list[str]:
    file_label = str(entry.get("file") or "(unknown file)")
    source_label = str(entry.get("source") or "current changes")
    why_text = truncate(str(entry.get("why") or "Updated as part of generated changes."), 320).replace("\n", " ")
    before_text = _sanitize_code_block_text(str(entry.get("before") or "(none)"))
    after_text = _sanitize_code_block_text(str(entry.get("after") or "(none)"))
    static_overhead = (
        len(file_label)
        + len(source_label)
        + len(why_text)
        + 250
    )
    code_budget = max(120, min(REVIEW_CODE_CHUNK_LIMIT, (REVIEW_MESSAGE_LIMIT - static_overhead) // 2))
    before_chunks = _split_text_for_code_block(before_text, code_budget)
    after_chunks = _split_text_for_code_block(after_text, code_budget)
    part_count = max(len(before_chunks), len(after_chunks))
    blocks: list[str] = []

    for part_idx in range(part_count):
        before_chunk = before_chunks[part_idx] if part_idx < len(before_chunks) else "(no additional before lines)"
        after_chunk = after_chunks[part_idx] if part_idx < len(after_chunks) else "(no additional after lines)"
        part_suffix = f" (part {part_idx + 1}/{part_count})" if part_count > 1 else ""
        blocks.append(
            "\n".join(
                [
                    f"**Major Change {index}/{total}{part_suffix}**",
                    f"File: `{file_label}`",
                    f"Source: `{source_label}`",
                    "Before:",
                    f"```text\n{before_chunk}\n```",
                    "After:",
                    f"```text\n{after_chunk}\n```",
                    f"Why: {why_text}",
                ]
            )
        )

    return blocks


async def send_major_change_review(
    channel: discord.abc.Messageable,
    title: str,
    entries: list[dict],
) -> None:
    if not entries:
        await channel.send(f"**{title}**\n(no major changes detected)")
        return

    blocks: list[str] = []
    total_entries = len(entries)
    for idx, entry in enumerate(entries, 1):
        blocks.extend(_format_review_entry_parts(entry, idx, total_entries))

    total_blocks = len(blocks)
    for idx, block in enumerate(blocks, 1):
        await channel.send(f"**{title}** · part {idx}/{total_blocks}\n{block}")


async def send_change_summary(
    channel: discord.abc.Messageable,
    title: str,
    lines: list[str],
) -> None:
    if not lines:
        await channel.send(f"**{title}**\n(no changes detected)")
        return

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for raw_line in lines:
        line = _truncate_inline_text(raw_line.replace("\n", " "), 500)
        line_len = len(line) + 1
        if current_lines and current_len + line_len > 1800:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = line_len
            continue
        current_lines.append(line)
        current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines))

    total_chunks = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        suffix = "" if total_chunks == 1 else f" · part {idx}/{total_chunks}"
        await channel.send(f"**{title}**{suffix}\n{chunk}")


async def send_working_session_wrapup(
    channel: discord.abc.Messageable,
    session: dict,
    cwd: str | None = None,
) -> None:
    lines = build_change_summary_lines(cwd, session=session)
    if lines:
        await send_change_summary(channel, f"Current changes on `{session['branch']}`", lines)
    else:
        await channel.send(f"📊 {get_diff_stat(cwd) or 'clean'}")
    await channel.send(_working_session_command_help(cwd))


async def send_engine_output_block(
    channel: discord.abc.Messageable,
    title: str,
    output: str,
    *,
    failure_notice: str | None = None,
) -> None:
    body = truncate(_sanitize_code_block_text(output or "(no output)"), 1800)
    try:
        await channel.send(f"**{title}:**\n```\n{body}\n```")
    except (discord.Forbidden, discord.HTTPException):
        notice = failure_notice or f"⚠️ {title} finished, but I couldn't post the engine output."
        try:
            await channel.send(notice)
        except (discord.Forbidden, discord.HTTPException):
            pass


def get_status_porcelain(path: str | None = None) -> list[str]:
    """Return git status --porcelain lines (empty if clean)."""
    raw = run_git(["git", "status", "--porcelain"], path).stdout.strip()
    if not raw:
        return []
    return [line for line in raw.split("\n") if line.strip()]


def get_diff_stat(path: str | None = None) -> str:
    """Short summary: 3 files changed, 12 insertions, 2 deletions."""
    base = _base_branch(path)
    stat = run_git(["git", "diff", "--stat", f"{base}...HEAD"], path).stdout.strip()
    # Also include any uncommitted changes
    uncommitted_stat = run_git(["git", "diff", "--stat"], path).stdout.strip()
    untracked = run_git(["git", "ls-files", "--others", "--exclude-standard"], path).stdout.strip()
    lines = []
    if stat:
        lines.append(stat.split("\n")[-1].strip())
    elif uncommitted_stat:
        lines.append(uncommitted_stat.split("\n")[-1].strip())
    if untracked:
        n = len(untracked.split("\n"))
        lines.append(f"{n} new file(s)")
    return ", ".join(lines) or "no changes"


def _rev_parse_head(path: str | None = None) -> str | None:
    result = run_git(["git", "rev-parse", "--verify", "HEAD"], path)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _inline_code_text(text: str, limit: int = 120) -> str:
    clean = _collapse_whitespace(_sanitize_discord_text(text).replace("`", "'"))
    if len(clean) > limit:
        clean = clean[: max(0, limit - 3)].rstrip() + "..."
    return f"`{clean or '(unknown)'}`"


def _format_pull_status_line(raw_line: str) -> str | None:
    parts = raw_line.split("\t")
    if len(parts) < 2:
        return None

    status = parts[0].strip()
    code = status[:1]
    labels = {
        "A": "added",
        "C": "copied",
        "D": "deleted",
        "M": "modified",
        "R": "renamed",
        "T": "type changed",
        "U": "unmerged",
        "X": "unknown",
        "B": "pairing broken",
    }
    label = labels.get(code, status.lower() or "changed")

    paths = [part.strip() for part in parts[1:] if part.strip()]
    if code in {"R", "C"} and len(paths) >= 2:
        return f"- {label} {_inline_code_text(paths[0])} -> {_inline_code_text(paths[1])}"
    if paths:
        return f"- {label} {_inline_code_text(paths[-1])}"
    return None


def build_pull_change_summary_lines(
    before_rev: str | None,
    after_rev: str | None,
    path: str | None = None,
) -> list[str]:
    if not before_rev or not after_rev or before_rev == after_rev:
        return []

    lines: list[str] = []
    count_result = run_git(["git", "rev-list", "--count", f"{before_rev}..{after_rev}"], path)
    commit_count: int | None = None
    if count_result.returncode == 0:
        try:
            commit_count = int(count_result.stdout.strip() or "0")
        except ValueError:
            commit_count = None

    shortstat = run_git(["git", "diff", "--shortstat", before_rev, after_rev], path).stdout.strip()
    headline_bits: list[str] = []
    if commit_count is not None:
        headline_bits.append(f"{commit_count} commit(s)")
    if shortstat:
        headline_bits.append(shortstat)
    lines.append(f"Summary: {', '.join(headline_bits) if headline_bits else 'working tree updated'}")

    log_limit = PULL_SUMMARY_COMMIT_LIMIT + 1
    log_result = run_git(
        ["git", "log", "--format=%h %s", f"--max-count={log_limit}", f"{before_rev}..{after_rev}"],
        path,
    )
    commits = [line.strip() for line in log_result.stdout.splitlines() if line.strip()]
    if commits:
        lines.extend(["", "Commits:"])
        for line in commits[:PULL_SUMMARY_COMMIT_LIMIT]:
            lines.append(f"- {_inline_code_text(line, 160)}")
        remaining = max(0, (commit_count or len(commits)) - PULL_SUMMARY_COMMIT_LIMIT)
        if remaining:
            lines.append(f"- ... and {remaining} more commit(s).")

    status_result = run_git(
        ["git", "diff", "--name-status", "--find-renames", before_rev, after_rev],
        path,
    )
    files = [
        formatted
        for formatted in (_format_pull_status_line(line) for line in status_result.stdout.splitlines())
        if formatted
    ]
    if files:
        lines.extend(["", "Files:"])
        lines.extend(files[:PULL_SUMMARY_FILE_LIMIT])
        remaining = len(files) - PULL_SUMMARY_FILE_LIMIT
        if remaining > 0:
            lines.append(f"- ... and {remaining} more file(s).")

    return lines


def get_ahead_count(path: str | None = None) -> int:
    """How many commits HEAD is ahead of the base branch."""
    base = _base_branch(path)
    result = run_git(["git", "rev-list", "--count", f"{base}..HEAD"], path)
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


# ── Model discovery ───────────────────────────────────────────────────────────

def get_codex_models() -> list[tuple[str, int | None]]:
    """Return available Codex models as (slug, context_window) tuples from the CLI's local cache."""
    cache = pathlib.Path.home() / ".codex" / "models_cache.json"
    try:
        with open(cache) as f:
            data = json.load(f)
        return [
            (m["slug"], m.get("context_window"))
            for m in data.get("models", [])
            if m.get("visibility") != "hidden"
        ]
    except Exception:
        return [
            ("gpt-5.5", None), ("gpt-5.4", None),
            ("gpt-5.4-mini", None), ("gpt-5.3-codex-spark", None),
        ]


def get_kimi_models() -> list[tuple[str, str | None]]:
    """Return available Kimi models as (alias, display_name) tuples from the CLI config."""
    home = pathlib.Path(os.environ.get("KIMI_CODE_HOME") or (pathlib.Path.home() / ".kimi-code"))
    try:
        with open(home / "config.toml", "rb") as f:
            data = tomllib.load(f)
        return [
            (alias, cfg.get("display_name") if isinstance(cfg, dict) else None)
            for alias, cfg in data.get("models", {}).items()
        ]
    except Exception:
        return [
            ("kimi-code/k3", None),
            ("kimi-code/kimi-for-coding", None),
            ("kimi-code/kimi-for-coding-highspeed", None),
        ]


async def get_claude_models() -> list[tuple[str, str]]:
    """Return available Claude models as (id, display_name) tuples from the Anthropic API."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        def _fetch():
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _fetch)
            return [(m["id"], m.get("display_name", m["id"])) for m in data.get("data", [])]
        except Exception:
            pass
    return [
        ("opus", "Claude Opus 4.6"),
        ("sonnet", "Claude Sonnet 4.6"),
        ("haiku", "Claude Haiku 4.5"),
    ]


def resolve_model_selector(
    selector: str,
    models: list[tuple[str, object]],
) -> tuple[str | None, str | None]:
    """
    Resolve model by 1-based index or exact model id.
    Returns (resolved_model, error_message).
    """
    token = selector.strip()
    if not token:
        return None, "Provide a model name or number."
    if token.isdigit():
        if not models:
            return None, "No models available."
        index = int(token)
        if index < 1 or index > len(models):
            return None, f"Model number must be between 1 and {len(models)}."
        return models[index - 1][0], None
    for model_id, _ in models:
        if model_id.lower() == token.lower():
            return model_id, None
    # Keep free-form names working for newly released models.
    return token, None


def split_model_reasoning_selector(selector: str) -> tuple[str | None, str | None, str | None]:
    """
    Split `model ... [reasoning ...]` into separate selectors.
    Returns (model_selector, reasoning_selector, error_message).
    """
    token = selector.strip()
    if not token:
        return None, None, "Provide a model name or number."
    if token.lower().startswith("reasoning "):
        return None, None, "Provide a model name or number before `reasoning`."
    if re.search(r"\s+reasoning\s*$", token, flags=re.IGNORECASE):
        return None, None, "Provide a reasoning level (name or number)."

    parts = re.split(r"\s+reasoning\s+", token, maxsplit=1, flags=re.IGNORECASE)
    model_selector = parts[0].strip()
    reasoning_selector = parts[1].strip() if len(parts) > 1 else None
    if not model_selector:
        return None, None, "Provide a model name or number."
    if len(parts) > 1 and not reasoning_selector:
        return None, None, "Provide a reasoning level (name or number)."
    return model_selector, reasoning_selector, None


def resolve_reasoning_selector(selector: str, engine: str) -> tuple[str | None, str | None]:
    """
    Resolve reasoning effort for an engine.
    Returns (resolved_effort_or_none_for_default, error_message).
    """
    token = selector.strip().lower()
    if not token:
        return None, "Provide a reasoning level (name or number)."

    if engine == "claude":
        options = CLAUDE_REASONING_OPTIONS
    elif engine == "kimi":
        options = KIMI_REASONING_OPTIONS
    else:
        options = CODEX_REASONING_OPTIONS
    label = {"claude": "Claude", "kimi": "Kimi"}.get(engine, "Codex")
    if token.isdigit():
        index = int(token)
        if index < 1 or index > len(options):
            return None, f"{label} reasoning number must be between 1 and {len(options)}."
        token = options[index - 1]

    if token in ("default", "auto", "unset", "clear", "none"):
        return None, None

    if engine == "claude":
        aliases = {"med": "medium"}
        token = aliases.get(token, token)
        if token not in CLAUDE_REASONING_LEVELS:
            choices = "`, `".join(CLAUDE_REASONING_OPTIONS)
            return None, f"Claude reasoning must be a number `1-{len(CLAUDE_REASONING_OPTIONS)}` or one of `{choices}`."
        return token, None

    if engine == "kimi":
        if token not in KIMI_REASONING_LEVELS:
            choices = "`, `".join(KIMI_REASONING_OPTIONS)
            return None, f"Kimi reasoning must be a number `1-{len(KIMI_REASONING_OPTIONS)}` or one of `{choices}`."
        return token, None

    aliases = {
        "min": "minimal",
        "med": "medium",
        "max": "xhigh",
        "veryhigh": "xhigh",
        "very-high": "xhigh",
    }
    token = aliases.get(token, token)
    if token not in CODEX_REASONING_LEVELS:
        choices = "`, `".join(CODEX_REASONING_OPTIONS)
        return None, f"Codex reasoning must be a number `1-{len(CODEX_REASONING_OPTIONS)}` or one of `{choices}`."
    return token, None


def format_reasoning_effort(effort: str | None) -> str:
    return effort or "default"


def format_reasoning_options_numbered(engine: str) -> str:
    if engine == "claude":
        options = CLAUDE_REASONING_OPTIONS
    elif engine == "kimi":
        options = KIMI_REASONING_OPTIONS
    else:
        options = CODEX_REASONING_OPTIONS
    return "\n".join(f"{idx}. `{level}`" for idx, level in enumerate(options, start=1))


# ── Login helpers ─────────────────────────────────────────────────────────────

async def login_codex(ch: discord.TextChannel) -> None:
    """Run `codex login --device-auth`, relay URL+code to Discord, wait for completion."""
    await ch.send("🔑 Starting Codex device login...")

    proc = await asyncio.create_subprocess_exec(
        "codex", "login", "--device-auth",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    output_lines = []
    sent_link = False

    async def read_stream(stream):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = strip_ansi(line.decode()).strip()
            if not text:
                continue
            output_lines.append(text)
            await ch.send(f"```\n{text}\n```")

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stream(proc.stdout), read_stream(proc.stderr)),
            timeout=120,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await ch.send("⏰ Login timed out (120s). Try again.")
        return

    if proc.returncode == 0:
        await ch.send("✅ Codex login successful!")
    else:
        combined = "\n".join(output_lines[-5:]) if output_lines else "(no output)"
        await ch.send(f"❌ Codex login failed (exit {proc.returncode}):\n```\n{combined}\n```")


async def login_claude(ch: discord.TextChannel) -> None:
    """Run `claude login`, relay the OAuth URL to Discord, wait for completion."""
    await ch.send("🔑 Starting Claude Code login...")

    proc = await asyncio.create_subprocess_exec(
        "claude", "login",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    output_lines = []

    async def read_stream(stream):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = strip_ansi(line.decode()).strip()
            if not text:
                continue
            output_lines.append(text)
            await ch.send(f"```\n{text}\n```")

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stream(proc.stdout), read_stream(proc.stderr)),
            timeout=120,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await ch.send("⏰ Login timed out (120s). Try again.")
        return

    if proc.returncode == 0:
        await ch.send("✅ Claude Code login successful!")
    else:
        combined = "\n".join(output_lines[-5:]) if output_lines else "(no output)"
        await ch.send(f"❌ Claude login failed (exit {proc.returncode}):\n```\n{combined}\n```")


async def login_kimi(ch: discord.TextChannel) -> None:
    """Run `kimi login`, relay the device-auth URL+code to Discord, wait for completion."""
    await ch.send("🔑 Starting Kimi Code login...")

    proc = await asyncio.create_subprocess_exec(
        "kimi", "login",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    output_lines = []

    async def read_stream(stream):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = strip_ansi(line.decode()).strip()
            if not text:
                continue
            output_lines.append(text)
            await ch.send(f"```\n{text}\n```")

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stream(proc.stdout), read_stream(proc.stderr)),
            timeout=120,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await ch.send("⏰ Login timed out (120s). Try again.")
        return

    if proc.returncode == 0:
        await ch.send("✅ Kimi Code login successful!")
    else:
        combined = "\n".join(output_lines[-5:]) if output_lines else "(no output)"
        await ch.send(f"❌ Kimi login failed (exit {proc.returncode}):\n```\n{combined}\n```")


# ── Engine runners ────────────────────────────────────────────────────────────

STATUS_REFRESH = 5  # seconds between live status updates


async def _stream_status_heartbeat(status_msg, label: str, start: float, render_tail) -> None:
    """Keep a quiet engine run visibly alive without imposing a deadline."""
    while True:
        await asyncio.sleep(STATUS_REFRESH)
        elapsed = int(time.time() - start)
        tail = strip_ansi(render_tail()).strip()
        if len(tail) > 1400:
            tail = tail[-1400:]
        try:
            await status_msg.edit(
                content=f"⏳ {label} working... ({elapsed}s)\n"
                f"```\n{tail or '(waiting for output...)'}\n```"
            )
        except discord.HTTPException:
            pass


async def _run_with_live_output(
    cmd: list[str],
    ch: discord.TextChannel,
    label: str,
    cwd: str | None = None,
) -> str:
    """Run a subprocess, live-updating a single Discord message with output."""
    status_msg = await ch.send(f"⚙️ {label} started...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd or REPO_PATH,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,  # 10MB – prevents "chunk longer than limit" on large output lines
    )
    running_procs[ch.id] = proc

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    live_chunks: list[bytes] = []
    done = asyncio.Event()
    start = time.time()

    async def read_stdout():
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            stdout_chunks.append(chunk)
            live_chunks.append(chunk)

    async def read_stderr():
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)
            live_chunks.append(chunk)

    async def live_update():
        last_text = ""
        while not done.is_set():
            await asyncio.sleep(STATUS_REFRESH)
            if done.is_set():
                break
            elapsed = int(time.time() - start)
            # Live status should include both streams: Codex frequently writes progress to stderr.
            raw = b"".join(live_chunks).decode(errors="replace")
            # Show the tail of the output that fits in a Discord message
            tail = strip_ansi(raw).strip()
            # Truncate to fit in Discord (2000 char limit minus formatting)
            if len(tail) > 1400:
                tail = tail[-1400:]
            if tail == last_text:
                # No new output, just update the timer
                try:
                    await status_msg.edit(content=f"⏳ {label} working... ({elapsed}s)\n```\n{tail or '(waiting for output...)'}\n```")
                except discord.HTTPException:
                    pass
                continue
            last_text = tail
            try:
                await status_msg.edit(content=f"⏳ {label} working... ({elapsed}s)\n```\n{tail}\n```")
            except discord.HTTPException:
                pass

    try:
        io_task = asyncio.gather(read_stdout(), read_stderr(), proc.wait())
        update_task = asyncio.create_task(live_update())
        await io_task
    finally:
        done.set()
        update_task.cancel()
        await asyncio.gather(update_task, return_exceptions=True)
        running_procs.pop(ch.id, None)
        elapsed = int(time.time() - start)
        try:
            await status_msg.edit(content=f"✅ {label} finished ({elapsed}s)\nI'll post the output below.")
        except discord.HTTPException:
            pass

    stdout = b"".join(stdout_chunks).decode(errors="replace")
    stderr = b"".join(stderr_chunks).decode(errors="replace")

    output = stdout or "(no output)"
    if proc.returncode != 0 and stderr:
        tail = stderr.strip().split("\n")[-5:]
        output += "\n\n⚠️ stderr (tail):\n" + "\n".join(tail)
    return output


async def _run_claude_streaming(
    cmd: list[str],
    ch: discord.TextChannel,
    label: str,
    cwd: str | None = None,
) -> str:
    """Run Claude Code with stream-json output, live-updating Discord as events arrive."""
    status_msg = await ch.send(f"⚙️ {label} started...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd or REPO_PATH,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,  # 10MB – prevents "chunk longer than limit" on large JSON lines
    )
    running_procs[ch.id] = proc

    accumulated_text: list[str] = []
    tool_activity: list[str] = []
    final_result: str | None = None
    stderr_chunks: list[bytes] = []
    result_usage: dict = {}
    start = time.time()

    def render_status_tail() -> str:
        display = "".join(accumulated_text)
        tool_line = f"[tools: {', '.join(tool_activity[-5:])}]\n" if tool_activity else ""
        return tool_line + display

    async def read_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    async def stream_stdout() -> None:
        nonlocal final_result
        async for raw_line in proc.stdout:
            line_str = raw_line.decode(errors="replace").strip()
            if not line_str:
                continue
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                accumulated_text.append(line_str + "\n")
                continue

            event_type = event.get("type")
            if event_type == "assistant":
                for item in event.get("message", {}).get("content", []):
                    if item.get("type") == "text":
                        accumulated_text.append(item["text"])
                    elif item.get("type") == "tool_use":
                        tool_activity.append(item["name"])
            elif event_type == "result":
                final_result = event.get("result", "")
                usage_data = event.get("usage") or {}
                if usage_data:
                    result_usage.update({
                        "input_tokens": usage_data.get("input_tokens", 0),
                        "output_tokens": usage_data.get("output_tokens", 0),
                        "cache_read": usage_data.get("cache_read_input_tokens", 0),
                        "cache_write": usage_data.get("cache_creation_input_tokens", 0),
                    })

    status_task = asyncio.create_task(
        _stream_status_heartbeat(status_msg, label, start, render_status_tail)
    )
    try:
        await asyncio.gather(stream_stdout(), read_stderr(), proc.wait())
    finally:
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)
        running_procs.pop(ch.id, None)
        elapsed = int(time.time() - start)
        try:
            await status_msg.edit(content=f"✅ {label} finished ({elapsed}s)\nI'll post the output below.")
        except discord.HTTPException:
            pass

    usage_snapshot = {"engine": "claude"}
    usage_snapshot.update(result_usage)
    channel_last_usage[ch.id] = usage_snapshot
    _accumulate_global_usage("claude", result_usage)

    stderr = b"".join(stderr_chunks).decode(errors="replace")
    output = final_result or "".join(accumulated_text) or "(no output)"
    if proc.returncode != 0 and stderr:
        tail_lines = stderr.strip().split("\n")[-5:]
        output += "\n\n⚠️ stderr (tail):\n" + "\n".join(tail_lines)
    return output


def _codex_usage_totals(usage: dict) -> dict[str, int]:
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read": int(
            usage.get("cached_input_tokens", usage.get("cache_read_input_tokens", 0)) or 0
        ),
        "cache_write": int(
            usage.get("cache_creation_input_tokens", usage.get("cache_write_input_tokens", 0)) or 0
        ),
    }


def _format_codex_activity(item: dict) -> str | None:
    item_type = str(item.get("type") or "").strip()
    if item_type == "command_execution":
        command = str(item.get("command") or "").strip()
        return f"cmd: {truncate(command, 120)}" if command else "cmd"
    if item_type in {"file_change", "file_diff"}:
        path = str(item.get("path") or item.get("file") or "").strip()
        return f"file: {path}" if path else "file change"
    if item_type in {"mcp_tool_call", "tool_call"}:
        name = str(item.get("name") or item.get("tool") or item.get("tool_name") or "").strip()
        server = str(item.get("server") or item.get("server_name") or "").strip()
        label = f"{server}/{name}" if server and name else name or server
        return f"tool: {label}" if label else "tool"
    if item_type == "web_search":
        query = str(item.get("query") or "").strip()
        return f"web: {truncate(query, 120)}" if query else "web search"
    if item_type == "error":
        message = str(item.get("message") or item.get("error") or "").strip()
        return f"error: {truncate(message, 120)}" if message else "error"
    return None


def _extract_codex_item_text(item: dict) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = item.get("content")
    if isinstance(content, list):
        parts = [
            str(part.get("text") or "").strip()
            for part in content
            if isinstance(part, dict) and str(part.get("text") or "").strip()
        ]
        if parts:
            return "\n".join(parts)
    message = item.get("message")
    if isinstance(message, dict):
        return _extract_codex_item_text(message)
    return ""


async def _run_codex_streaming(
    cmd: list[str],
    ch: discord.TextChannel,
    label: str,
    cwd: str | None = None,
) -> str:
    """Run Codex with --json, live-updating Discord from JSONL events."""
    status_msg = await ch.send(f"⚙️ {label} started...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd or REPO_PATH,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
    )
    running_procs[ch.id] = proc

    agent_messages: list[str] = []
    raw_lines: list[str] = []
    activity: list[str] = []
    errors: list[str] = []
    stderr_chunks: list[bytes] = []
    result_usage: dict[str, int] = {}
    codex_thread_id = ""
    usage_accumulated = False
    start = time.time()

    def render_status_tail() -> str:
        display = "\n\n".join(agent_messages).strip()
        activity_line = f"[activity: {' | '.join(activity[-5:])}]\n" if activity else ""
        thread_line = f"[thread: {codex_thread_id}]\n" if codex_thread_id else ""
        return thread_line + activity_line + display

    def remember_thread_id(value: object | None) -> None:
        nonlocal codex_thread_id
        thread_id = _clean_codex_thread_id(value)
        if not thread_id:
            return
        codex_thread_id = thread_id
        run_ctx = active_run_contexts.get(ch.id)
        if isinstance(run_ctx, dict):
            run_ctx["codex_thread_id"] = thread_id

    def snapshot_usage() -> None:
        nonlocal usage_accumulated
        snapshot: dict[str, object] = {"engine": "codex"}
        if codex_thread_id:
            snapshot["codex_thread_id"] = codex_thread_id
        snapshot.update(result_usage)
        channel_last_usage[ch.id] = snapshot
        if result_usage and not usage_accumulated:
            _accumulate_global_usage("codex", result_usage)
            usage_accumulated = True

    async def read_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    async def stream_stdout() -> None:
        nonlocal result_usage
        async for raw_line in proc.stdout:
            line_str = raw_line.decode(errors="replace").strip()
            if not line_str:
                continue
            raw_lines.append(line_str)
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                agent_messages.append(line_str)
                continue

            event_type = str(event.get("type") or "").strip()
            thread_obj = event.get("thread")
            thread_id = event.get("thread_id") or event.get("threadId")
            if not thread_id and isinstance(thread_obj, dict):
                thread_id = thread_obj.get("id")
            remember_thread_id(thread_id)

            if event_type == "thread.started":
                remember_thread_id(event.get("thread_id") or event.get("threadId"))
            elif event_type in {"item.started", "item.completed"}:
                item = event.get("item")
                if isinstance(item, dict):
                    item_activity = _format_codex_activity(item)
                    if item_activity:
                        activity.append(item_activity)
                    item_type = str(item.get("type") or "").strip()
                    if event_type == "item.completed" and (
                        item_type in {"agent_message", "assistant_message", "message"}
                        or item_type.endswith("_message")
                    ):
                        text = _extract_codex_item_text(item)
                        if text:
                            agent_messages.append(text)
            elif event_type == "turn.completed":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    result_usage = _codex_usage_totals(usage)
            elif event_type in {"turn.failed", "error"}:
                err_obj = event.get("error")
                if isinstance(err_obj, dict):
                    message = str(err_obj.get("message") or err_obj).strip()
                else:
                    message = str(err_obj or event.get("message") or "").strip()
                if message:
                    errors.append(message)

    status_task = asyncio.create_task(
        _stream_status_heartbeat(status_msg, label, start, render_status_tail)
    )
    try:
        await asyncio.gather(stream_stdout(), read_stderr(), proc.wait())
    finally:
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)
        snapshot_usage()
        running_procs.pop(ch.id, None)
        elapsed = int(time.time() - start)
        try:
            await status_msg.edit(content=f"✅ {label} finished ({elapsed}s)\nI'll post the output below.")
        except discord.HTTPException:
            pass

    stderr = b"".join(stderr_chunks).decode(errors="replace")
    output = "\n\n".join(agent_messages).strip() or "\n".join(raw_lines).strip() or "(no output)"
    if errors:
        output += "\n\n⚠️ Codex error:\n" + "\n".join(errors[-3:])
    if proc.returncode != 0 and stderr:
        tail_lines = stderr.strip().split("\n")[-5:]
        output += "\n\n⚠️ stderr (tail):\n" + "\n".join(tail_lines)
    return output


def _kimi_usage_totals(session_id: str, start_ms: int) -> dict[str, int]:
    """Sum Kimi wire.jsonl usage.record events at/after start_ms. Zeros on any failure."""
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
    if not session_id:
        return totals
    home = pathlib.Path(os.environ.get("KIMI_CODE_HOME") or (pathlib.Path.home() / ".kimi-code"))
    try:
        wires = list(home.glob(f"sessions/*/{session_id}/agents/main/wire.jsonl"))
        for wire in wires:
            with open(wire, "rb") as f:
                for raw in f:
                    try:
                        event = json.loads(raw)
                        if event.get("type") != "usage.record":
                            continue
                        if int(event.get("time") or 0) < start_ms:
                            continue
                        usage = event.get("usage") or {}
                        totals["input_tokens"] += int(usage.get("inputOther") or 0)
                        totals["output_tokens"] += int(usage.get("output") or 0)
                        totals["cache_read"] += int(usage.get("inputCacheRead") or 0)
                        totals["cache_write"] += int(usage.get("inputCacheCreation") or 0)
                    except Exception:
                        continue
    except Exception:
        pass
    return totals


async def _run_kimi_streaming(
    cmd: list[str],
    ch: discord.TextChannel,
    label: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Run Kimi Code with stream-json output, live-updating Discord as events arrive."""
    status_msg = await ch.send(f"⚙️ {label} started...")

    start_ms = int(time.time() * 1000)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd or REPO_PATH,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,  # 10MB – prevents "chunk longer than limit" on large JSON lines
    )
    running_procs[ch.id] = proc

    accumulated_text: list[str] = []
    tool_activity: list[str] = []
    final_result: str | None = None
    stderr_chunks: list[bytes] = []
    kimi_session_id = ""
    usage_accumulated = False
    start = time.time()

    def render_status_tail() -> str:
        display = "".join(accumulated_text)
        tool_line = f"[tools: {', '.join(tool_activity[-5:])}]\n" if tool_activity else ""
        return tool_line + display

    def snapshot_usage() -> None:
        nonlocal usage_accumulated
        totals = _kimi_usage_totals(kimi_session_id, start_ms)
        channel_last_usage[ch.id] = {"engine": "kimi", **totals}
        if not usage_accumulated:
            _accumulate_global_usage("kimi", totals)
            usage_accumulated = True

    async def read_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    async def stream_stdout() -> None:
        nonlocal final_result, kimi_session_id
        async for raw_line in proc.stdout:
            line_str = raw_line.decode(errors="replace").strip()
            if not line_str:
                continue
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                accumulated_text.append(line_str + "\n")
                continue

            role = event.get("role")
            if role == "assistant":
                content = event.get("content")
                if isinstance(content, str) and content.strip():
                    accumulated_text.append(content if content.endswith("\n") else content + "\n")
                    final_result = content
                for tool_call in event.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if isinstance(function, dict):
                        name = str(function.get("name") or "").strip()
                        if name:
                            tool_activity.append(name)
            elif role == "meta":
                session_id = event.get("session_id")
                if isinstance(session_id, str) and session_id.strip():
                    kimi_session_id = session_id.strip()

    status_task = asyncio.create_task(
        _stream_status_heartbeat(status_msg, label, start, render_status_tail)
    )
    try:
        await asyncio.gather(stream_stdout(), read_stderr(), proc.wait())
    finally:
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)
        snapshot_usage()
        running_procs.pop(ch.id, None)
        elapsed = int(time.time() - start)
        try:
            await status_msg.edit(content=f"✅ {label} finished ({elapsed}s)\nI'll post the output below.")
        except discord.HTTPException:
            pass

    stderr = b"".join(stderr_chunks).decode(errors="replace")
    output = final_result or "".join(accumulated_text).strip() or "(no output)"
    if proc.returncode != 0 and stderr:
        tail_lines = stderr.strip().split("\n")[-5:]
        output += "\n\n⚠️ stderr (tail):\n" + "\n".join(tail_lines)
    return output

async def run_claude_code(
    task: str,
    ch: discord.TextChannel,
    resume: bool = False,
    images: list[str] | None = None,
    cwd: str | None = None,
    runtime_config: dict[str, str | None] | None = None,
) -> str:
    """Run Claude Code. If resume=True, uses --continue to continue last session."""
    model = get_model_for_engine("claude", runtime_config=runtime_config, ch_id=ch.id)
    reasoning_effort = get_reasoning_for_engine("claude", runtime_config=runtime_config, ch_id=ch.id)
    if images:
        img_lines = "\n".join(f"- {p}" for p in images)
        task = f"Examine the image(s) at the following path(s) using the Read tool:\n{img_lines}\n\n{task}"
    cmd = ["claude"]
    if resume:
        cmd.extend(["--continue", "-p", task])
    else:
        cmd.extend(["-p", task])

    cmd.extend([
        "--model", model,
        "--verbose",
        "--output-format", "stream-json",
        "--max-turns", "25",
        "--append-system-prompt",
        "You MUST implement changes, not just read or analyze files. "
        "Do not stop after reading context — use Edit/Write tools to "
        "make the requested code changes. If the task asks you to add, "
        "modify, or fix something, you must edit the relevant files.",
    ])
    if reasoning_effort:
        cmd.extend(["--effort", reasoning_effort])
    if CLAUDE_ALLOWED_TOOLS:
        cmd.append("--allowedTools")
        cmd.extend(CLAUDE_ALLOWED_TOOLS)
    if CLAUDE_DENIED_TOOLS:
        cmd.append("--disallowedTools")
        cmd.extend(CLAUDE_DENIED_TOOLS)

    return await _run_claude_streaming(cmd, ch, "Claude Code", cwd=cwd)


async def run_codex(
    task: str,
    ch: discord.TextChannel,
    resume: bool = False,
    images: list[str] | None = None,
    cwd: str | None = None,
    runtime_config: dict[str, str | None] | None = None,
) -> str:
    """Run Codex CLI. If resume=True, resumes the saved thread ID when available."""
    model = get_model_for_engine("codex", runtime_config=runtime_config, ch_id=ch.id)
    reasoning_effort = get_reasoning_for_engine("codex", runtime_config=runtime_config, ch_id=ch.id)
    if resume:
        codex_thread_id = _current_codex_thread_id(ch.id)
        run_ctx = active_run_contexts.get(ch.id)
        if isinstance(run_ctx, dict) and codex_thread_id:
            run_ctx["codex_thread_id"] = codex_thread_id
        cmd = [
            "codex", "exec",
            "--sandbox", "workspace-write",
            "resume",
            "--model", model,
            "--json",
        ]
        if not codex_thread_id:
            cmd.append("--last")
    else:
        codex_thread_id = ""
        cmd = [
            "codex", "exec",
            "--sandbox", "workspace-write",
            "--model", model,
            "--json",
        ]
    if reasoning_effort:
        cmd.extend(["-c", f"model_reasoning_effort=\"{reasoning_effort}\""])
    if images:
        cmd.extend(["--image", ",".join(images)])
    if resume and codex_thread_id:
        cmd.append(codex_thread_id)
    cmd.append(task)

    return await _run_codex_streaming(cmd, ch, "Codex CLI", cwd=cwd)


async def run_kimi(
    task: str,
    ch: discord.TextChannel,
    resume: bool = False,
    images: list[str] | None = None,
    cwd: str | None = None,
    runtime_config: dict[str, str | None] | None = None,
) -> str:
    """Run Kimi Code. If resume=True, uses -c to continue the last session in this cwd."""
    model = get_model_for_engine("kimi", runtime_config=runtime_config, ch_id=ch.id)
    reasoning_effort = get_reasoning_for_engine("kimi", runtime_config=runtime_config, ch_id=ch.id)
    if images:
        img_lines = "\n".join(f"- {p}" for p in images)
        task = f"Examine the image(s) at the following path(s) using the Read tool:\n{img_lines}\n\n{task}"
    # Kimi has no --append-system-prompt flag, so prepend the same implement-mandate
    # that run_claude_code passes via --append-system-prompt.
    task = (
        "You MUST implement changes, not just read or analyze files. "
        "Do not stop after reading context — use Edit/Write tools to "
        "make the requested code changes. If the task asks you to add, "
        "modify, or fix something, you must edit the relevant files.\n\n"
        + task
    )
    cmd = ["kimi"]
    if resume:
        cmd.append("-c")
    cmd.extend(["-p", task, "--output-format", "stream-json", "-m", model])

    env = None
    if reasoning_effort:
        env = dict(os.environ)
        env["KIMI_MODEL_THINKING_EFFORT"] = reasoning_effort

    return await _run_kimi_streaming(cmd, ch, "Kimi Code", cwd=cwd, env=env)


async def run_engine(
    engine: str,
    task: str,
    ch: discord.TextChannel,
    resume: bool = False,
    images: list[str] | None = None,
    cwd: str | None = None,
    stop_event: asyncio.Event | None = None,
    runtime_config: dict[str, str | None] | None = None,
) -> str:
    if engine == "codex":
        runner = run_codex
    elif engine == "kimi":
        runner = run_kimi
    else:
        runner = run_claude_code
    run_config = _coerce_runtime_config(runtime_config, fallback=get_runtime_config(ch.id))
    run_id = f"{ch.id}-{time.time_ns()}"

    if stop_event and stop_event.is_set():
        return "(stopped)"

    raw_task = task
    active_run_contexts[ch.id] = {
        "run_id": run_id,
        "engine": engine,
        "cwd": cwd,
        "task": raw_task,
        "resume": bool(resume),
        "started_at": int(time.time()),
    }
    try:
        if resume:
            task = build_resume_prompt(task, ch.id, cwd, engine)
        # Prevent stale usage from a previous run leaking into this turn/session.
        channel_last_usage[ch.id] = {"engine": engine}

        output = await runner(task, ch, resume, images, cwd=cwd, runtime_config=run_config)

        if stop_event and stop_event.is_set():
            return "(stopped)"
        if output is None:
            output = "(no output)"

        queued_entries = pop_queued_run_commands(ch.id, run_id=run_id)
        if queued_entries:
            save_resume_context(ch.id, cwd, engine, raw_task, output, reason="queued_followup")
            queued_task, queued_images = build_queued_followup_task(queued_entries)
            await ch.send(
                f"📥 Processing {len(queued_entries)} queued follow-up instruction(s) from this run..."
            )
            next_output = await run_engine(
                engine,
                queued_task,
                ch,
                resume=True,
                images=queued_images or None,
                cwd=cwd,
                stop_event=stop_event,
                runtime_config=run_config,
            )
            if next_output == "(stopped)":
                return "(stopped)"
            output = f"{output}\n\n[queued follow-up]\n\n{next_output}"

        clear_resume_context(ch.id)
        return output
    finally:
        active_run_contexts.pop(ch.id, None)


# ── Git workflow ──────────────────────────────────────────────────────────────

def create_branch(
    task: str,
    engine: str,
    path: str | None = None,
    base_branch: str | None = None,
    agent_id: int | str | None = None,
) -> str:
    # Same-engine agents can start identical tasks in the same second. Add the
    # channel/thread identity and a nonce so their branches cannot collide.
    agent_key = re.sub(r"[^a-zA-Z0-9]+", "", str(agent_id or os.getpid()))[-8:] or "agent"
    nonce = f"{time.time_ns():x}"[-10:]
    branch = f"{BRANCH_PREFIX}/{engine}/{slugify(task)}-{agent_key}-{nonce}"
    # Prefer the provided base branch (for saved plan execution), else resolve fallback.
    preferred_base = (base_branch or "").strip()
    base = None
    canonical = _canonical_repo(path or REPO_PATH)
    if preferred_base and _ensure_local_branch(preferred_base, canonical):
        base = preferred_base
    if not base:
        base = _resolve_checkout_branch(canonical)
    if not base:
        raise RuntimeError("No base branch found (missing dev/main and no local branches).")
    # Fetch the base so the new branch is up-to-date. Use canonical repo for fetch
    # to avoid "branch already checked out in another worktree" errors.
    run_git(["git", "fetch", "origin", base], canonical)
    # Create feature branch directly from origin/<base> — works in worktrees
    # without needing to checkout the base branch first.
    create = run_git(["git", "checkout", "-b", branch, f"origin/{base}"], path)
    if create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip() or "branch creation failed"
        raise RuntimeError(f"Branch creation failed for `{branch}` from `origin/{base}`: {err}")
    return branch


def _commit_clause_for_unit(unit: dict) -> tuple[int, str] | None:
    kind = str(unit.get("kind") or "hunk")
    file_label = str(unit.get("file") or "(unknown file)")
    if kind == "hunk":
        before_text = str(unit.get("before") or "")
        after_text = str(unit.get("after") or "")
        before_definitions = _summary_definition_targets(before_text)
        after_definitions = _summary_definition_targets(after_text)
        added_definitions = [item for item in after_definitions if item not in before_definitions]
        removed_definitions = [item for item in before_definitions if item not in after_definitions]
        updated_definitions = [item for item in after_definitions if item in before_definitions]

        definition_actions: list[str] = []
        for verb, definitions in (
            ("added", added_definitions),
            ("removed", removed_definitions),
            ("updated", updated_definitions),
        ):
            grouped = _summary_group_definitions(definitions)
            if grouped:
                definition_actions.append(f"{verb} {grouped}")
        if definition_actions:
            priority = 0 if added_definitions or removed_definitions else 1
            return priority, _summary_join_phrases(definition_actions)

        hunk_target = _summary_target_from_text(str(unit.get("hunk_context") or ""), file_label)
        if hunk_target:
            if before_text and after_text:
                return 2, f"updated {hunk_target}"
            if after_text:
                return 2, f"added changes to {hunk_target}"
            if before_text:
                return 2, f"removed changes from {hunk_target}"

    clause = _summary_clause_for_unit(unit)
    if not clause:
        return None
    if kind == "rename":
        before_path = str(unit.get("before") or unit.get("before_path") or "").strip()
        after_path = str(unit.get("after") or unit.get("after_path") or "").strip()
        clause = f"renamed `{before_path}` to `{after_path}`"
    elif clause in {"added this new file"} or clause.startswith("added content to this "):
        clause = f"added `{file_label}`"
    elif clause in {"removed this file"} or clause.startswith("removed content from this "):
        clause = f"removed `{file_label}`"
    elif clause.startswith("updated part of this "):
        clause = f"updated `{file_label}`"
    elif clause == "updated the binary contents":
        clause = f"updated binary contents in `{file_label}`"
    elif clause == "changed file metadata":
        clause = f"changed metadata for `{file_label}`"
    return 3 + _summary_clause_priority(clause), clause


def build_commit_description(path: str | None = None) -> str:
    """Build a concise description of the changes currently staged for commit."""
    diff = run_git(
        ["git", "diff", "--cached", "--unified=0", "--find-renames", "--no-color"],
        path,
    ).stdout.strip()
    units = _parse_unified_review_units(diff, "staged vs HEAD") if diff else []

    descriptions: list[tuple[int, str]] = []
    seen: set[str] = set()
    for unit in units:
        result = _commit_clause_for_unit(unit)
        if not result:
            continue
        priority, clause = result
        normalized = _collapse_whitespace(clause).rstrip(".")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        descriptions.append((priority, normalized))

    if descriptions:
        descriptions.sort(key=lambda item: item[0])
        selected = [description for _, description in descriptions[:3]]

        def join_selected() -> str:
            if len(selected) == 1:
                return selected[0]
            if len(selected) == 2:
                return f"{selected[0]}; {selected[1]}"
            return f"{selected[0]}; {selected[1]}; and {selected[2]}"

        summary = join_selected().replace("`", "")
        while len(summary) > 160 and len(selected) > 1:
            selected.pop()
            summary = join_selected().replace("`", "")
        summary = _truncate_inline_text(summary, 160)
        return summary[:1].upper() + summary[1:]

    changed_files = run_git(
        ["git", "diff", "--cached", "--name-only", "--find-renames"],
        path,
    ).stdout.splitlines()
    files = [file_name.strip() for file_name in changed_files if file_name.strip()]
    if len(files) == 1:
        return f"Update {files[0]}"
    if files:
        return f"Update {len(files)} files"
    return "Update repository files"


def auto_commit(turn: int, path: str | None = None) -> None:
    """Commit any pending changes as a WIP save after each engine turn."""
    cwd = path or REPO_PATH
    try:
        add = run_git(["git", "add", "."], path)
    except Exception:
        logger.exception("auto_commit failed during git add (turn=%s, path=%s)", turn, cwd)
        return
    if add.returncode != 0:
        err = (add.stderr or add.stdout or "").strip() or f"exit {add.returncode}"
        logger.error("auto_commit git add failed (turn=%s, path=%s): %s", turn, cwd, truncate(err, 500))
        return

    try:
        status_res = run_git(["git", "status", "--porcelain"], path)
    except Exception:
        logger.exception("auto_commit failed during git status (turn=%s, path=%s)", turn, cwd)
        return
    if status_res.returncode != 0:
        err = (status_res.stderr or status_res.stdout or "").strip() or f"exit {status_res.returncode}"
        logger.error("auto_commit git status failed (turn=%s, path=%s): %s", turn, cwd, truncate(err, 500))
        return

    if status_res.stdout.strip():
        description = build_commit_description(path)
        try:
            commit = run_git(["git", "commit", "-m", f"WIP (turn {turn}): {description}"], path)
        except Exception:
            logger.exception("auto_commit failed during git commit (turn=%s, path=%s)", turn, cwd)
            return
        if commit.returncode != 0:
            err = (commit.stderr or commit.stdout or "").strip() or f"exit {commit.returncode}"
            logger.error(
                "auto_commit git commit failed (turn=%s, path=%s): %s",
                turn,
                cwd,
                truncate(err, 500),
            )


async def commit_and_push(branch: str, path: str | None = None) -> str:
    # Commit any remaining uncommitted changes
    run_git(["git", "add", "."], path)
    status = run_git(["git", "status", "--porcelain"], path).stdout.strip()
    if status:
        description = build_commit_description(path)
        run_git(["git", "commit", "-m", f"auto: {description}"], path)
    push = run_git(["git", "push", "-u", "origin", branch], path)
    if push.returncode == 0:
        return f"✅ Pushed to `{branch}`"
    return f"❌ Push failed:\n```\n{push.stderr[-500:]}\n```"


async def discard_changes(branch: str, path: str | None = None) -> str | None:
    canonical = _canonical_repo(path or REPO_PATH)
    # A failed agent sync may leave an in-progress merge in this worktree.
    run_git(["git", "merge", "--abort"], path)
    run_git(["git", "checkout", "."], path)
    run_git(["git", "clean", "-fd"], path)
    target = _resolve_checkout_branch(canonical, avoid=branch)
    if target:
        # In worktree context, checking out target may fail if it's used elsewhere;
        # that's fine — the worktree will be removed by _end_session.
        run_git(["git", "checkout", target], path)
    current = current_branch(path)
    if is_protected_branch(branch):
        return current or target
    if current and current != branch:
        # Delete feature branch from canonical repo so it works even if worktree is detached
        run_git(["git", "branch", "-D", branch], canonical)
    return current or target


# ── Merge / PR ────────────────────────────────────────────────────────────────

def _detach_if_worktree_on_branch(branch: str, path: str | None = None) -> None:
    """Detach the provided worktree if it's currently on the given branch."""
    if not path:
        return
    # Never detach the canonical repo checkout unexpectedly.
    if _canonical_repo(path) == path:
        return
    if not pathlib.Path(path).exists():
        return
    if current_branch(path) == branch:
        run_git(["git", "checkout", "--detach"], path)


def _missing_branch_error(msg: str) -> bool:
    low = msg.lower()
    return ("branch" in low and "not found" in low) or ("remote ref does not exist" in low)


def sync_agent_branch(source: str, target: str, path: str) -> tuple[bool, str]:
    """Merge the latest target into one agent worktree.

    A clean sync commits normally. A content conflict remains only in this
    agent's worktree so its next engine turn can resolve the marked files.
    """
    canonical = _canonical_repo(path)
    fetch = run_git(["git", "fetch", "origin", target], canonical)
    if fetch.returncode != 0:
        msg = (fetch.stderr or fetch.stdout or "fetch failed").strip()
        return False, f"❌ Could not fetch `{target}`:\n```\n{truncate(msg, 500)}\n```"

    target_ref = f"origin/{target}"
    if run_git(
        ["git", "rev-parse", "--verify", f"{target_ref}^{{commit}}"], canonical
    ).returncode != 0:
        return False, f"❌ Target branch `{target}` not found on `origin`."
    if current_branch(path) != source:
        return False, f"❌ Agent worktree is not on its expected branch `{source}`."

    merge = run_git(["git", "merge", target_ref, "--no-edit"], path)
    if merge.returncode == 0:
        return True, f"✅ Synced `{source}` with the latest `{target}` in this agent worktree."

    conflicts = run_git(
        ["git", "diff", "--name-only", "--diff-filter=U"], path
    ).stdout.splitlines()
    conflict_files = [file_name.strip() for file_name in conflicts if file_name.strip()]
    if not conflict_files:
        msg = (merge.stderr or merge.stdout or "merge failed").strip()
        run_git(["git", "merge", "--abort"], path)
        return False, f"❌ Could not sync `{target}`:\n```\n{truncate(msg, 500)}\n```"

    listing = ", ".join(f"`{name}`" for name in conflict_files[:8])
    if len(conflict_files) > 8:
        listing += f", and {len(conflict_files) - 8} more"
    return False, (
        f"⚠️ Latest `{target}` is staged for reconciliation only in this agent worktree.\n"
        f"Conflicting files: {listing}\n"
        "Send a follow-up asking the agent to resolve the integration conflicts, then review again. "
        "Use `abort` to discard the agent branch instead."
    )


async def merge_branch(source: str, target: str, path: str | None = None) -> str:
    canonical = _canonical_repo(path or REPO_PATH)
    lock_key = str(pathlib.Path(canonical).resolve())
    lock = _repo_integration_locks.setdefault(lock_key, asyncio.Lock())

    # Agents edit concurrently, but integrations into the same repository are
    # intentionally one-at-a-time. The merge happens in a disposable detached
    # worktree, leaving the canonical checkout and agent worktrees untouched.
    async with lock:
        fetch = run_git(["git", "fetch", "origin", "--prune"], canonical)
        if fetch.returncode != 0:
            msg = (fetch.stderr or fetch.stdout or "fetch failed").strip()
            return f"❌ Could not refresh `origin` before merging:\n```\n{truncate(msg, 500)}\n```"

        source_ref = source
        if run_git(
            ["git", "rev-parse", "--verify", f"{source_ref}^{{commit}}"], canonical
        ).returncode != 0:
            source_ref = f"origin/{source}"
        if run_git(
            ["git", "rev-parse", "--verify", f"{source_ref}^{{commit}}"], canonical
        ).returncode != 0:
            return f"❌ Source branch `{source}` not found locally or on `origin`."

        target_ref = f"origin/{target}"
        if run_git(
            ["git", "rev-parse", "--verify", f"{target_ref}^{{commit}}"], canonical
        ).returncode != 0:
            target_ref = target
        if run_git(
            ["git", "rev-parse", "--verify", f"{target_ref}^{{commit}}"], canonical
        ).returncode != 0:
            return f"❌ Target branch `{target}` not found locally or on `origin`."

        integration_path = str(
            _worktree_base(canonical)
            / f".integrate-{slugify(target, 20)}-{os.getpid()}-{time.time_ns()}"
        )
        added = run_git(
            ["git", "worktree", "add", "--detach", integration_path, target_ref],
            canonical,
        )
        if added.returncode != 0:
            msg = (added.stderr or added.stdout or "worktree creation failed").strip()
            return f"❌ Could not create an isolated integration worktree:\n```\n{truncate(msg, 500)}\n```"

        try:
            merge = run_git([
                "git", "merge", source_ref, "--no-ff",
                "-m", f"merge {source} into {target}",
            ], integration_path)
            if merge.returncode != 0:
                msg = merge.stdout or merge.stderr or "unknown error"
                run_git(["git", "merge", "--abort"], integration_path)
                return (
                    f"❌ Integration conflict `{source}` → `{target}`:\n"
                    f"```\n{truncate(msg, 500)}\n```\n"
                    "The target and all agent worktrees are unchanged; the source branch was kept."
                )

            push = run_git(
                ["git", "push", "origin", f"HEAD:refs/heads/{target}"],
                integration_path,
            )
            if push.returncode != 0:
                msg = (push.stderr or push.stdout or "push failed").strip()
                return (
                    "❌ Isolated merge completed, but the target changed before it could be pushed. "
                    "No agent worktree was modified; retry the merge.\n"
                    f"```\n{truncate(msg, 500)}\n```"
                )
        finally:
            run_git(["git", "worktree", "remove", integration_path, "--force"], canonical)
            run_git(["git", "worktree", "prune"], canonical)

        result = f"✅ Merged `{source}` → `{target}` in isolation and pushed."

        # Delete an integrated feature branch only after the target push succeeds.
        if source.startswith(f"{BRANCH_PREFIX}/"):
            if is_protected_branch(source):
                result += f"\n🛡️ Protected branch; skipping delete for `{source}`."
            else:
                _detach_if_worktree_on_branch(source, path)
                local_delete = run_git(["git", "branch", "-D", source], canonical)
                remote_delete = run_git(["git", "push", "origin", "--delete", source], canonical)
                local_msg = (local_delete.stderr or local_delete.stdout or "").strip()
                remote_msg = (remote_delete.stderr or remote_delete.stdout or "").strip()
                local_ok = local_delete.returncode == 0 or _missing_branch_error(local_msg)
                remote_ok = remote_delete.returncode == 0 or _missing_branch_error(remote_msg)
                if local_ok and remote_ok:
                    result += f"\n🗑️ Deleted branch `{source}`."
                else:
                    if not local_ok:
                        result += (
                            f"\n⚠️ Local delete failed for `{source}`:\n"
                            f"```\n{truncate(local_msg or 'unknown error', 200)}\n```"
                        )
                    if not remote_ok:
                        result += (
                            f"\n⚠️ Remote delete failed for `{source}`:\n"
                            f"```\n{truncate(remote_msg or 'unknown error', 200)}\n```"
                        )

        run_git(["git", "fetch", "origin", target], canonical)
        return result


# ── Worktree helpers ──────────────────────────────────────────────────────

def _worktree_base(repo_path: str) -> pathlib.Path:
    return pathlib.Path(repo_path) / ".worktrees"


def _worktree_path(repo_path: str, channel_id: int) -> str:
    return str(_worktree_base(repo_path) / f"ch-{channel_id}")


def _canonical_repo(path: str) -> str:
    """If path is a worktree under .worktrees/, return the parent repo. Else return path as-is."""
    p = pathlib.Path(path)
    if p.parent.name == ".worktrees":
        return str(p.parent.parent)
    return path


def _worktree_registered(repo_path: str, worktree_path: str) -> tuple[bool, str]:
    """Return whether worktree_path is present in `git worktree list --porcelain` output."""
    listed = run_git(["git", "worktree", "list", "--porcelain"], repo_path)
    if listed.returncode != 0:
        err = (listed.stderr or listed.stdout or "").strip() or f"exit {listed.returncode}"
        return False, f"Failed to list worktrees: {err}"

    target = pathlib.Path(worktree_path)
    try:
        target = target.resolve()
    except OSError:
        pass
    target_str = str(target)

    for line in listed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = pathlib.Path(line.removeprefix("worktree ").strip())
        try:
            candidate = candidate.resolve()
        except OSError:
            pass
        if str(candidate) == target_str:
            return True, ""
    return False, "worktree path not present in `git worktree list --porcelain` output"


def ensure_worktree(repo_path: str, channel_id: int) -> str:
    """Create or reuse a worktree for this channel. Returns worktree path."""
    canonical = _canonical_repo(repo_path)
    wt_path = _worktree_path(canonical, channel_id)
    if pathlib.Path(wt_path).exists():
        ok, err = _worktree_registered(canonical, wt_path)
        if not ok:
            raise RuntimeError(f"Existing worktree verification failed for `{wt_path}`: {err}")
        return wt_path
    pathlib.Path(wt_path).parent.mkdir(parents=True, exist_ok=True)
    # Use --detach so we don't conflict with any branch checked out elsewhere
    result = run_git(["git", "worktree", "add", "--detach", wt_path], canonical)
    if result.returncode != 0:
        raise RuntimeError(f"Worktree creation failed: {(result.stderr or result.stdout or '').strip()}")
    ok, err = _worktree_registered(canonical, wt_path)
    if not ok:
        raise RuntimeError(f"Worktree add succeeded but verification failed for `{wt_path}`: {err}")
    return wt_path


def activate_session_on_branch(
    ch_id: int,
    repo_path: str,
    branch: str,
    engine: str,
    description: str,
    runtime_config: dict[str, str | None] | None = None,
    turns: int = 1,
    followups: list[str] | None = None,
    total_usage: dict[str, int] | None = None,
    codex_thread_id: str | None = None,
) -> dict:
    canonical = _canonical_repo(repo_path)
    if not pathlib.Path(canonical).exists():
        raise RuntimeError(f"Saved repo not found: `{canonical}`")
    if not branch:
        raise RuntimeError("Saved branch is missing.")
    if not _ensure_local_branch(branch, canonical):
        raise RuntimeError(f"Saved branch `{branch}` not found.")

    wt_path = ensure_worktree(canonical, ch_id)
    checkout = run_git(["git", "checkout", branch], wt_path)
    if checkout.returncode != 0:
        err = (checkout.stderr or checkout.stdout or "").strip() or "checkout failed"
        raise RuntimeError(f"Could not checkout `{branch}`: {err}")

    saved_runtime = _coerce_runtime_config(runtime_config, fallback=get_runtime_config(ch_id))
    try:
        turn_count = int(turns)
    except (TypeError, ValueError):
        turn_count = 1
    turn_count = max(0, turn_count)
    session = {
        "branch": branch,
        "engine": _normalize_engine_name(engine),
        "description": description.strip() or "recovered session",
        "turns": turn_count,
        "phase": "working",
        "cwd": wt_path,
        "runtime_config": dict(saved_runtime),
    }
    cleaned_followups = _clean_string_list(followups, limit=12)
    if cleaned_followups:
        session["followups"] = cleaned_followups
    totals = _coerce_usage_totals(total_usage)
    if totals:
        session["total_usage"] = totals
    clean_thread_id = _clean_codex_thread_id(codex_thread_id)
    if session["engine"] == "codex" and clean_thread_id:
        session["codex_thread_id"] = clean_thread_id

    active_sessions[ch_id] = session
    channel_cwd[ch_id] = wt_path
    record_state(ch_id, wt_path, branch)
    return session


def restore_unfinished_session(ch_id: int) -> tuple[dict | None, str | None]:
    snapshot = load_unfinished_task_snapshot(ch_id)
    if not snapshot:
        return None, "No saved unfinished task for this channel."

    repo = str(snapshot.get("repo") or _canonical_repo(str(snapshot.get("cwd") or ""))).strip()
    branch = str(snapshot.get("branch") or "").strip()
    if branch == "?":
        branch = ""
    engine = _normalize_engine_name(snapshot.get("engine"))
    description = str(
        snapshot.get("official_task")
        or snapshot.get("intent")
        or snapshot.get("task")
        or "resumed session"
    ).strip()
    followups = snapshot.get("followups")
    turns = snapshot.get("turns")
    total_usage = snapshot.get("total_usage")
    runtime_config = snapshot.get("runtime_config")
    codex_thread_id = _clean_codex_thread_id(snapshot.get("codex_thread_id"))

    try:
        session = activate_session_on_branch(
            ch_id,
            repo,
            branch,
            engine,
            description,
            runtime_config=runtime_config if isinstance(runtime_config, dict) else None,
            turns=turns if isinstance(turns, int) else 1,
            followups=followups if isinstance(followups, list) else None,
            total_usage=total_usage if isinstance(total_usage, dict) else None,
            codex_thread_id=codex_thread_id,
        )
    except Exception as exc:
        return None, str(exc)
    return session, None


def remove_worktree(repo_path: str, channel_id: int) -> None:
    """Remove a channel's worktree after session ends."""
    canonical = _canonical_repo(repo_path)
    wt_path = _worktree_path(canonical, channel_id)
    if pathlib.Path(wt_path).exists():
        run_git(["git", "worktree", "remove", wt_path, "--force"], canonical)


def prune_worktrees(repo_path: str) -> None:
    """Clean up stale worktree references."""
    run_git(["git", "worktree", "prune"], _canonical_repo(repo_path))


def _end_session(ch_id: int, cwd: str) -> None:
    """Clean up session: remove worktree, reset channel_cwd, delete session."""
    canonical = _canonical_repo(cwd)
    remove_worktree(canonical, ch_id)
    channel_cwd[ch_id] = canonical
    active_sessions.pop(ch_id, None)
    clear_unfinished_task_snapshot(ch_id)


async def create_pr(source: str, target: str, title: str, path: str | None = None) -> str:
    if not has_gh_cli():
        return "❌ `gh` not installed. Run `sudo apt install gh && gh auth login`."

    result = run_git([
        "gh", "pr", "create",
        "--base", target, "--head", source,
        "--title", title,
        "--body", f"Auto-generated from Discord bot.\n\nTask: {title}",
    ], path)
    if result.returncode == 0:
        return f"✅ PR created: {result.stdout.strip()}"
    return f"❌ PR failed:\n```\n{result.stderr[-500:]}\n```"


# ── Discord handlers ─────────────────────────────────────────────────────────

HELP_TEXT_1_TEMPLATE = """**Starting a session:**
`<task>` — default engine ({default}) · `claude: <task>` / `cc:` / `claude code:` · `codex: <task>` / `cx:` / `openai:` · `kimi: <task>` / `km:`
Use separate Discord channels/threads to run agents concurrently; `agents` lists them
`plan: <task>` — planning mode with default engine/model (saves/extends plan context)
`plan: do [extra instructions]` / `plan do [extra instructions]` — execute saved plan context, then clear it
`plan show` — show saved plan context · `plan clear` / `clear plan` — clear saved plan context

**During a session:**
Type follow-ups freely — engine keeps context
`stop` — cancel the current run
`add: <instruction>` / `queue: <instruction>` — queue work during an active run (auto-resumes after finish)
`sync [target]` — bring latest target (default: dev) into only this agent worktree
`switch <branch|N>` — save & switch branch (creates if new)
`cwd <n>` — save & switch repo mid-session
`diff` — quick raw peek · `review` — major changes (before/after/why) · `undo` — discard uncommitted changes
`resume` — reopen a saved unfinished recovery session · `resume show` — inspect it
`context clear` — forget saved resume context, unfinished snapshot, and queued follow-ups (`resume clear` / `clear context`)

**Ending a session:**
`done` — show descriptive per-file summary + push prompt
`yes` / `push` — commit + push, then merge · `no` / `discard` — discard
`abort` — discard immediately · `skip` — skip merge step

**After pushing:**
`merge <target>` — merge current/session/last-pushed into target
`merge src>tgt` / `merge src into tgt` — explicit source & target
`pr <target>` — open a pull request"""


def help_text_1(ch_id: int | None = None) -> str:
    return HELP_TEXT_1_TEMPLATE.format(default=f"{get_default_engine(ch_id)} · this channel")

HELP_TEXT_2 = """**Branches:**
`branches` — list branches (use `N` in commands)
`branch delete|del <name|N> [local|remote|both] [force]`
`branch protect [list|add|remove|clear|reset]`
`switch|branch switch <branch|N>` — switch branch (auto-commit if in session)

**Recovery:**
`resume` — reopen saved unfinished recovery session · `resume show` — inspect it
`recover` — list orphaned branches · `recover <id>` — resume
`recover drop <id>` — delete orphaned branch

**Multi-repo:**
`repos` · `cwd` / `cwd <n>` — show or switch active repo
`repo <n> status|diff|review|commit [msg]|push|branches`

**Config (channel-scoped by default):**
`engine` — show this channel config (+ global default)
`engine global` — show global default config
`claude models` · `codex models` · `kimi models` — list available models (numbered)
`claude model <n|name>` · `codex model <n|name>` · `kimi model <n|name>` — set model for this channel
`engine claude|codex|kimi` · `engine claude model <n|name> [reasoning <n|level>]` · `engine codex model <n|name> [reasoning <n|level>]` · `engine kimi model <n|name> [reasoning <n|level>]` — set this channel
`engine global claude|codex|kimi` · `engine global claude|codex|kimi model <n|name> [reasoning <n|level>]` — set global default
`claude reasoning [n|level]` · `codex reasoning [n|level]` · `kimi reasoning [n|level]` — set reasoning for this channel
`engine claude reasoning <n|level>` · `engine codex reasoning <n|level>` · `engine kimi reasoning <n|level>` — set this channel
`engine global claude|codex|kimi reasoning <n|level>` — set global default reasoning
`reasoning|default reasoning [n|level]` — view/set this channel default-engine reasoning
`model|default model <n|name>` — set model for this channel default engine

**Info:**
`status` — current branch and working tree
`agents` — list concurrent agents across Discord channels/threads
`usage` — engine/session token usage + remaining limits (best effort)
`branches` — list branches (use `N` references)
`pull [branch|N]` — pull latest changes with commit/file summary
`doctor` — run CLI/repo diagnostics
`help` — refresh pinned command reference

**Login:** `claude|cc login` · `codex|cx|openai login` · `kimi|km login` · `login both`
**System:** `restart`"""

HELP_PIN_TITLE_1 = "Help (1/2)"
HELP_PIN_TITLE_2 = "Help (2/2)"


def _help_embed(title: str, text: str) -> discord.Embed:
    return discord.Embed(title=title, description=text)


async def ensure_pinned_help(channel: discord.abc.Messageable) -> bool:
    """Ensure help messages are pinned and up to date. Returns True if changed."""
    changed = False
    channel_id = getattr(channel, "id", None)
    current_help_1 = help_text_1(channel_id if isinstance(channel_id, int) else None)

    # Fetch existing pins — if forbidden, fall back to plain text
    help_by_title: dict[str, list[discord.Message]] = {
        HELP_PIN_TITLE_1: [],
        HELP_PIN_TITLE_2: [],
    }
    try:
        async for msg in channel.pins():
            if not msg.embeds or msg.author.id != client.user.id:
                continue
            title = msg.embeds[0].title
            if title in help_by_title:
                help_by_title[title].append(msg)
    except (AttributeError, discord.Forbidden, discord.HTTPException):
        await channel.send(current_help_1)
        await channel.send(HELP_TEXT_2)
        return True

    def _latest(msgs: list[discord.Message]) -> discord.Message | None:
        return max(msgs, key=lambda m: m.created_at) if msgs else None

    pinned_1 = _latest(help_by_title[HELP_PIN_TITLE_1])
    pinned_2 = _latest(help_by_title[HELP_PIN_TITLE_2])

    # Remove duplicate pins
    for title, msgs in help_by_title.items():
        keep = pinned_1 if title == HELP_PIN_TITLE_1 else pinned_2
        for msg in msgs:
            if keep and msg.id == keep.id:
                continue
            try:
                await msg.unpin(reason="Superseded help pin")
                changed = True
            except (discord.Forbidden, discord.HTTPException):
                pass

    # Update or create each help message
    for pinned, title, text in [
        (pinned_1, HELP_PIN_TITLE_1, current_help_1),
        (pinned_2, HELP_PIN_TITLE_2, HELP_TEXT_2),
    ]:
        if pinned:
            existing = pinned.embeds[0].description or ""
            if existing != text:
                try:
                    await pinned.edit(embed=_help_embed(title, text))
                    changed = True
                except (discord.Forbidden, discord.HTTPException):
                    pass
        else:
            try:
                msg = await channel.send(embed=_help_embed(title, text))
                await msg.pin(reason=title)
                changed = True
            except (discord.Forbidden, discord.HTTPException):
                await channel.send(text)
                changed = True

    return changed


@tree.command(name="help", description="Show all available bot commands")
async def slash_help(interaction: discord.Interaction):
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("Not authorised.", ephemeral=True)
        return
    await interaction.response.send_message("Help is pinned at the top of the channel.", ephemeral=True)
    if interaction.channel:
        await ensure_pinned_help(interaction.channel)


@client.event
async def on_ready():
    await tree.sync()
    ssh_ok = check_github_ssh()
    claude_ok, claude_status = check_claude_cli()
    codex_ok, codex_status = check_codex_cli()
    kimi_ok, kimi_status = check_kimi_cli()
    global_config = get_runtime_config(None)
    print(f"🤖 Bot online as {client.user}")
    print(f"   Allowed user  : {ALLOWED_USER_ID}")
    print(f"   Default engine: {global_config['default_engine']} (global)")
    print(
        f"   Claude: {global_config['claude_model']} · "
        f"Codex: {global_config['codex_model']} · "
        f"Kimi: {global_config['kimi_model']} (global)"
    )
    print(f"   Channel runtime overrides: {len(CHANNEL_RUNTIME_CONFIGS)}")
    print(f"   gh CLI        : {'yes' if has_gh_cli() else 'no'}")
    print(f"   GitHub SSH    : {'yes' if ssh_ok else '⚠️  FAILED'}")
    print(f"   Claude CLI    : {claude_status}")
    print(f"   Codex CLI     : {codex_status}")
    print(f"   Kimi CLI      : {kimi_status}")
    codex_trusted = _load_codex_trusted_dirs()
    print(f"   Project dirs  :")
    for label, path in GIT_PROJECTS:
        p = pathlib.Path(path)
        if not p.exists():
            print(f"     ⚠️  [{label}] directory not found: {path}")
        elif run_git(["git", "rev-parse", "--git-dir"], path).returncode != 0:
            print(f"     ⚠️  [{label}] not a git repo: {path}")
        else:
            branch = current_branch(path)
            # Claude: trust is stored in .claude/settings.local.json inside the project dir
            # Codex: trust_level="trusted" must be set in ~/.codex/config.toml
            claude_tag = "trusted" if _is_claude_trusted(path) else "⚠️ NOT TRUSTED"
            codex_tag  = "trusted" if _normalize_path(path) in codex_trusted else "⚠️ NOT TRUSTED"
            print(f"     ✓  [{label}] on '{branch}' — claude: {claude_tag}  codex: {codex_tag}")
            print(f"          {path}")
    if not ssh_ok:
        print(f"\n⚠️  Cannot connect to GitHub via SSH.")
        print(f"   Fix:   eval \"$(ssh-agent -s)\" && ssh-add ~/.ssh/id_ed25519")
        print(f"   Test:  ssh -T git@github.com")
    if not claude_ok:
        print(f"\n⚠️  Claude CLI unavailable: {claude_status}")
    if not codex_ok:
        print(f"\n⚠️  Codex CLI unavailable: {codex_status}")
    if not kimi_ok:
        print(f"\n⚠️  Kimi CLI unavailable: {kimi_status}")
    claude_untrusted = [path for _, path in GIT_PROJECTS
                        if not _is_claude_trusted(path)]
    if claude_untrusted and claude_ok:
        print(f"\n⚠️  Claude is NOT trusted in {len(claude_untrusted)} project dir(s).")
        print(f"   Fix: run `claude` once interactively in each dir and approve trust.")
    codex_untrusted = [path for _, path in GIT_PROJECTS if _normalize_path(path) not in codex_trusted]
    if codex_untrusted and codex_ok:
        print(f"\n⚠️  Codex is NOT trusted in {len(codex_untrusted)} project dir(s).")
        print(f"   Codex will hang waiting for interactive input in those dirs.")
        print(f"   Fix: run `codex` once interactively in each dir and approve trust.")
    print(f"   Slash commands synced")
    for guild in client.guilds:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                try:
                    await ensure_pinned_help(ch)
                except Exception:
                    pass
    await _send_restart_confirmation()
    await _send_restore_notice()


async def _send_restart_confirmation():
    """If a restart was requested, notify the requesting channel."""
    if _RESTART_FLAG.exists():
        try:
            ch_id = int(_RESTART_FLAG.read_text().strip())
            _RESTART_FLAG.unlink()
            ch = client.get_channel(ch_id)
            if ch:
                await ch.send("✅ Bot restarted successfully.")
        except Exception:
            pass


async def _send_restore_notice():
    """Notify last active channel with restored cwd/branch on startup."""
    if not STATE_FILE.exists():
        return
    ch_id, cwd, branch, checkout_error = restore_state()
    if not ch_id or not cwd or not branch:
        return
    ch = client.get_channel(ch_id)
    if not ch:
        return
    msg = f"📍 Restored repo: `{cwd}`\n🌿 Restored branch: `{branch}`"
    if checkout_error:
        msg += f"\n⚠️ Could not checkout branch: `{checkout_error}`"
    await ch.send(msg)
    snapshot = load_unfinished_task_snapshot(ch_id)
    if snapshot:
        saved_engine = _normalize_engine_name(snapshot.get("engine"))
        saved_model = str(snapshot.get("model") or "?").strip() or "?"
        saved_branch = str(snapshot.get("branch") or "?").strip() or "?"
        await ch.send(
            "🧩 Saved unfinished recovery session found.\n"
            f"Engine: `{saved_engine}` · Model: `{saved_model}`\n"
            f"Branch: `{saved_branch}`\n"
            "Use `resume` to reopen it or `resume show` to inspect the saved snapshot."
        )


@client.event
async def on_resumed():
    print(f"🤖 Bot reconnected (session resumed) as {client.user}")
    await _send_restart_confirmation()


@client.event
async def on_message(message: discord.Message):
    global _restart_on_close
    if message.author.bot or not is_authorised(message):
        return

    content = message.content.strip()
    has_images = any(
        pathlib.Path(att.filename).suffix.lower() in IMAGE_EXTENSIONS
        for att in message.attachments
    )
    if not content and not has_images:
        return

    ch = message.channel
    lower = content.lower()
    session = active_sessions.get(ch.id)
    # Resolve active cwd: prefer session's cwd, then channel's, then default
    cwd = (session or {}).get("cwd") or channel_cwd.get(ch.id) or REPO_PATH
    record_state(ch.id, cwd)

    # ── Stop current run ────────────────────────────────────────────────
    if lower == "stop":
        stop_event = stop_events.get(ch.id)
        proc = running_procs.get(ch.id)
        if stop_event:
            stop_event.set()
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        if stop_event or proc:
            await ch.send("🛑 Stopped current run.")
        else:
            await ch.send("No active run to stop.")
        return

    # ── Concurrent agent overview (available even while this channel runs) ────
    if lower in ("agents", "agent status"):
        agent_ids = sorted(set(active_sessions) | set(active_run_contexts))
        if not agent_ids:
            await ch.send(
                "No active agents. Start tasks in separate Discord channels or threads to run them concurrently."
            )
            return
        lines = [f"🤖 **Active agents ({len(agent_ids)})**"]
        for agent_id in agent_ids:
            agent_session = active_sessions.get(agent_id) or {}
            run_ctx = active_run_contexts.get(agent_id) or {}
            engine = _normalize_engine_name(run_ctx.get("engine") or agent_session.get("engine"))
            branch = str(agent_session.get("branch") or "").strip()
            agent_cwd = str(
                run_ctx.get("cwd") or agent_session.get("cwd") or channel_cwd.get(agent_id) or ""
            ).strip()
            if not branch and agent_cwd and pathlib.Path(agent_cwd).exists():
                branch = current_branch(agent_cwd)
            if run_ctx:
                state = "running"
            elif agent_session.get("phase") == "review":
                state = "awaiting approval"
            else:
                state = "ready for follow-up"
            description = str(
                run_ctx.get("task") or agent_session.get("description") or ""
            ).strip()
            line = f"• <#{agent_id}> · **{engine}** · {state}"
            if branch:
                line += f" · `{branch}`"
            if description:
                line += f"\n  {truncate(description, 140)}"
            lines.append(line)
        await ch.send(truncate("\n".join(lines), 1900))
        return

    # ── Queue follow-up while run is active ───────────────────────────────
    proc = running_procs.get(ch.id)
    run_in_progress = bool(proc and proc.returncode is None)
    if run_in_progress:
        queue_match = re.match(r"^(add|queue)\s*:\s*(.*)$", content, flags=re.IGNORECASE)
        if queue_match:
            queued_command = (queue_match.group(2) or "").strip()
            queued_images = await download_attachments(message)
            if not queued_command and not queued_images:
                await ch.send("Usage while running: `add: <instruction>` (or attach image(s) with it).")
                return
            if session and queued_command:
                _record_session_followup(session, queued_command)

            run_ctx = active_run_contexts.get(ch.id) or {}
            run_id = str(run_ctx.get("run_id") or "").strip() or None
            run_engine_name = str(run_ctx.get("engine") or (session or {}).get("engine") or get_default_engine(ch.id))
            run_cwd = run_ctx.get("cwd") or cwd
            run_task = str(run_ctx.get("task") or queued_command or "queued follow-up")
            save_resume_context(ch.id, run_cwd, run_engine_name, run_task, None, reason="queued_followup")
            save_queued_run_command(ch.id, queued_command, queued_images, run_id=run_id)
            queue_len = queued_run_command_count(ch.id, run_id=run_id)
            await ch.send(
                f"📥 Queued follow-up #{queue_len} for the current run. "
                "I’ll resume automatically when it finishes."
            )
            return

        await ch.send(
            "⏳ A run is already in progress. Send `add: <instruction>` (or `queue:`) to append work, "
            "or `stop` to cancel."
        )
        return

    if lower == "resume show":
        snapshot = load_unfinished_task_snapshot(ch.id)
        if not snapshot:
            await ch.send("No saved unfinished task snapshot for this channel.")
            return
        ts = snapshot.get("ts")
        if isinstance(ts, (int, float)):
            saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        else:
            saved_at = "unknown"
        saved_engine = _normalize_engine_name(snapshot.get("engine"))
        saved_runtime = snapshot.get("runtime_config") if isinstance(snapshot.get("runtime_config"), dict) else None
        saved_model = str(snapshot.get("model") or "?").strip() or "?"
        saved_reasoning = format_reasoning_effort(
            get_reasoning_for_engine(saved_engine, runtime_config=saved_runtime, ch_id=ch.id)
        )
        saved_repo = str(snapshot.get("repo") or snapshot.get("cwd") or "(none)").strip() or "(none)"
        saved_branch = str(snapshot.get("branch") or "?").strip() or "?"
        saved_task = str(snapshot.get("official_task") or snapshot.get("task") or "(empty)").strip() or "(empty)"
        saved_intent = str(snapshot.get("intent") or saved_task).strip() or "(empty)"
        saved_diff = str(snapshot.get("diff_stat") or "no changes").strip() or "no changes"
        saved_reason = str(snapshot.get("reason") or "timeout_exhausted").strip() or "timeout_exhausted"
        saved_turns = snapshot.get("turns")
        saved_codex_thread_id = _clean_codex_thread_id(snapshot.get("codex_thread_id"))
        ac_snapshot = snapshot.get("auto_commit") if isinstance(snapshot.get("auto_commit"), dict) else {}
        auto_commit_subject = str(ac_snapshot.get("subject") or "").strip()
        auto_commit_sha = str(ac_snapshot.get("sha") or "").strip()
        auto_commit_created = ac_snapshot.get("created")
        auto_commit_line = "Auto-commit: `(unknown)`"
        if auto_commit_subject or auto_commit_sha:
            auto_commit_line = "Auto-commit: "
            if isinstance(auto_commit_created, bool):
                auto_commit_line += "created" if auto_commit_created else "reused HEAD"
            else:
                auto_commit_line += "saved"
            if auto_commit_subject:
                auto_commit_line += f" · {auto_commit_subject}"
            if auto_commit_sha:
                auto_commit_line += f" · `{auto_commit_sha[:12]}`"
        details = (
            "🧩 **Saved unfinished task**\n"
            f"Saved: `{saved_at}`\n"
            f"Reason: `{saved_reason}`\n"
            f"Engine: `{saved_engine}` · Model: `{saved_model}` · Reasoning: `{saved_reasoning}`\n"
            f"Repo: `{saved_repo}`\n"
            f"Branch: `{saved_branch}`\n"
            f"Task: {truncate(saved_task, 300)}\n"
            f"Intent: {truncate(saved_intent, 300)}\n"
        )
        if isinstance(saved_turns, int):
            details += f"Turns completed: `{saved_turns}`\n"
        if saved_codex_thread_id:
            details += f"Codex thread: `{saved_codex_thread_id}`\n"
        details += f"Diff: `{saved_diff}`\n{auto_commit_line}"
        await ch.send(details)
        output_tail = str(snapshot.get("output_tail") or "").strip()
        if output_tail:
            await ch.send(f"```\n{truncate(output_tail, 1800)}\n```")
        return

    if lower == "resume":
        snapshot = load_unfinished_task_snapshot(ch.id)
        if not snapshot:
            await ch.send("No saved unfinished task snapshot for this channel.")
            return
        if session:
            await ch.send(
                f"An active session is already open on `{session['branch']}`. "
                "Use `diff`, `review`, or send a follow-up directly."
            )
            return
        restored_session, err = restore_unfinished_session(ch.id)
        if err or not restored_session:
            await ch.send(f"❌ Could not restore unfinished session: `{err or 'unknown error'}`")
            return
        restored_runtime = get_session_runtime_config(restored_session, ch.id)
        restored_model = get_model_for_engine(
            restored_session["engine"],
            runtime_config=restored_runtime,
            ch_id=ch.id,
        )
        stat = get_diff_stat(restored_session["cwd"])
        intent = str(snapshot.get("intent") or snapshot.get("official_task") or restored_session["description"]).strip()
        restored_thread_line = ""
        if restored_session.get("codex_thread_id"):
            restored_thread_line = f"🧵 Codex thread: `{restored_session['codex_thread_id']}`\n"
        await ch.send(
            f"♻️ Restored unfinished session on `{restored_session['branch']}`\n"
            f"🧠 `{restored_session['engine']}` (`{restored_model}`)\n"
            f"📍 `{restored_session['cwd']}`\n"
            f"{restored_thread_line}"
            f"📌 {truncate(intent, 300)}\n"
            f"📊 {stat}\n"
            f"{_working_session_guidance(restored_session['cwd'])}"
        )
        return

    # ── Session: review (major changes: before/after/why) ─────────────────
    if lower == "review" and session:
        entries = build_major_change_review(cwd, session=session)
        await send_major_change_review(ch, f"Major changes on `{session['branch']}`", entries)
        return
    if lower == "review":
        await ch.send("No active session to review. Start a task or use `repo <n> review`.")
        return

    # ── Session: done → show descriptive per-file summary and prompt ─────
    if lower == "done" and session:
        lines = build_change_summary_lines(cwd, session=session)
        await send_change_summary(ch, f"Change summary on `{session['branch']}`", lines)
        if not lines:
            base = _base_branch(cwd)
            ahead = get_ahead_count(cwd)
            if ahead <= 0:
                await ch.send("No changes to commit.")
                return
            await ch.send(
                f"ℹ️ Working tree clean but branch is {ahead} commit(s) ahead of `{base}`. "
                "Continuing to the push prompt."
            )
        session["phase"] = "review"
        await ch.send(f"Reply {_review_action_prompt(cwd, bold=True)}.")
        return

    # ── Session: push approval (only at the `done` prompt; otherwise it's a follow-up) ──
    if lower in ("yes", "approve", "push", "lgtm", "ship it") and (
        not session or session.get("phase") == "review"
    ):
        if session and session.get("phase") == "review":
            await ch.send("⏳ Committing and pushing...")
            result = await commit_and_push(session["branch"], cwd)
            await ch.send(result)
            if "✅" in result:
                last_pushed[ch.id] = session["branch"]
                canonical = _canonical_repo(cwd)
                dev_exists = run_git(["git", "rev-parse", "--verify", DEV_BRANCH], canonical).returncode == 0
                if dev_exists:
                    await ch.send(f"⏳ Merging into `{DEV_BRANCH}`...")
                    merge_result = await merge_branch(session["branch"], DEV_BRANCH, cwd)
                    await ch.send(merge_result)
                    if merge_result.startswith("✅"):
                        _end_session(ch.id, cwd)
                        record_state(ch.id, canonical)
                        await ch.send("`pr main` to create a PR to main")
                    else:
                        session["phase"] = "working"
                        _, sync_result = sync_agent_branch(
                            session["branch"], DEV_BRANCH, cwd
                        )
                        await ch.send(sync_result)
                else:
                    branches = [b for b in run_git(
                        ["git", "branch", "--sort=-committerdate", "--format=%(refname:short)"], canonical
                    ).stdout.strip().split("\n") if b and b != session["branch"]]
                    listing = "\n".join(f"• `{b}`" for b in branches[:10])
                    session["phase"] = "merge_target"
                    await ch.send(
                        f"⚠️ No `{DEV_BRANCH}` branch found. Which branch should `{session['branch']}` merge into?\n{listing}\nReply with the branch name, or `skip` to skip merging."
                    )
            else:
                canonical = _canonical_repo(cwd)
                fallback = _resolve_checkout_branch(canonical, avoid=session["branch"])
                if fallback:
                    run_git(["git", "checkout", fallback], canonical)
                    record_state(ch.id, canonical, fallback)
                _end_session(ch.id, cwd)
            return
        # If no session but maybe old-style pending
        await ch.send("No session awaiting approval. Send `done` first to see the change summary.")
        return

    if lower == "skip" and session and session.get("phase") == "review":
        await ch.send("⏳ Committing and pushing (skip merge)...")
        result = await commit_and_push(session["branch"], cwd)
        await ch.send(result)
        canonical = _canonical_repo(cwd)
        if "✅" in result:
            last_pushed[ch.id] = session["branch"]
            record_state(ch.id, canonical, session["branch"])
            _end_session(ch.id, cwd)
            await ch.send("⏭️ Skipped merge. Use `merge <target>` or `pr <target>` any time.")
        else:
            fallback = _resolve_checkout_branch(canonical, avoid=session["branch"])
            if fallback:
                run_git(["git", "checkout", fallback], canonical)
                record_state(ch.id, canonical, fallback)
            _end_session(ch.id, cwd)
        return

    # ── Session: merge target selection ───────────────────────────────────
    if session and session.get("phase") == "merge_target":
        if lower == "skip":
            canonical = _canonical_repo(cwd)
            record_state(ch.id, canonical, session["branch"])
            await ch.send("⏭️ Skipped merge. Use `merge <branch>` or `pr <branch>` any time.")
            _end_session(ch.id, cwd)
            return
        target_input = content.strip()
        canonical = _canonical_repo(cwd)
        target = resolve_branch_case_insensitive(target_input, canonical) or target_input
        check = run_git(["git", "rev-parse", "--verify", target], canonical)
        if check.returncode != 0:
            # If there are multiple case-insensitive matches, ask for exact name.
            branches = get_branch_list(canonical)
            ci_matches = [b for b in branches if b.lower() == target_input.lower()]
            if len(ci_matches) > 1:
                listing = "\n".join(f"• `{b}`" for b in ci_matches[:10])
                await ch.send(
                    f"Multiple branches match `{target_input}` (case-insensitive). "
                    f"Reply with the exact branch name:\n{listing}"
                )
                return
            branches = [b for b in run_git(
                ["git", "branch", "--sort=-committerdate", "--format=%(refname:short)"], canonical
            ).stdout.strip().split("\n") if b and b != session["branch"]]
            listing = "\n".join(f"• `{b}`" for b in branches[:10])
            await ch.send(f"Branch `{target}` not found. Pick one:\n{listing}\nOr `skip` to skip.")
            return
        await ch.send(f"⏳ Merging into `{target}`...")
        merge_result = await merge_branch(session["branch"], target, cwd)
        await ch.send(merge_result)
        if merge_result.startswith("✅"):
            _end_session(ch.id, cwd)
            record_state(ch.id, canonical)
        else:
            session["phase"] = "working"
            _, sync_result = sync_agent_branch(session["branch"], target, cwd)
            await ch.send(sync_result)
        return

    # ── Session: discard (only at the `done` prompt; otherwise it's a follow-up) ──
    if lower in ("no", "reject", "discard", "nah") and (
        not session or session.get("phase") == "review"
    ):
        if session and session.get("phase") == "review":
            base = await discard_changes(session["branch"], cwd)
            _end_session(ch.id, cwd)
            if base and base != session["branch"]:
                await ch.send(f"🗑️ Discarded, back on `{base}`.")
            elif base:
                await ch.send(f"🗑️ Discarded. Still on `{base}` (no base branch found).")
            else:
                await ch.send("🗑️ Discarded changes. No base branch found to switch to.")
            return
        await ch.send("No session awaiting approval. Use `abort` to end an active session.")
        return

    # ── Session: abort (discard immediately) ──────────────────────────────
    if lower == "abort":
        if session:
            base = await discard_changes(session["branch"], cwd)
            _end_session(ch.id, cwd)
            if base and base != session["branch"]:
                await ch.send(f"🗑️ Session aborted, back on `{base}`.")
            elif base:
                await ch.send(f"🗑️ Session aborted. Still on `{base}` (no base branch found).")
            else:
                await ch.send("🗑️ Session aborted. No base branch found to switch to.")
        else:
            await ch.send("No active session.")
        return

    # ── Session: diff (peek at current changes) ──────────────────────────
    if lower == "diff" and session:
        diff = get_diff(cwd)
        stat = get_diff_stat(cwd)
        await ch.send(f"**Changes so far** ({stat}):\n"
                       f"```diff\n{truncate(diff, 1800)}\n```")
        return

    # ── Session: undo (revert uncommitted changes from last run) ──────────
    if lower == "undo" and session:
        run_git(["git", "merge", "--abort"], cwd)
        run_git(["git", "checkout", "."], cwd)
        run_git(["git", "clean", "-fd"], cwd)
        session["turns"] = max(0, session["turns"] - 1)
        await ch.send(
            "↩️ Reverted last changes. Send another instruction, `diff` for a quick peek, "
            "or `review` for major changes."
        )
        return

    # ── Merge commands ────────────────────────────────────────────────────
    if lower == "sync" or lower.startswith("sync "):
        if not session:
            await ch.send("`sync [target]` requires an active agent session.")
            return
        target_input = content[4:].strip() or DEV_BRANCH
        target = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(
            target_input.lower(), target_input
        )
        if target == session["branch"]:
            await ch.send(f"❌ Agent and target are the same branch: `{target}`")
            return
        await ch.send(f"⏳ Syncing this agent with the latest `{target}`...")
        _, sync_result = sync_agent_branch(session["branch"], target, cwd)
        session["phase"] = "working"
        await ch.send(sync_result)
        return

    if lower.startswith("merge "):
        aliases = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}
        target_str = content[6:].strip()  # preserve original case
        target_lower = target_str.lower()

        def _resolve_merge_ref(ref: str) -> str:
            ref = ref.strip()
            low = ref.lower()
            if low in aliases:
                return aliases[low]
            if ref.startswith("#") or ref.lstrip("#").isdigit():
                resolved = resolve_branch(ref, ch.id, cwd)
                return resolved or ref
            return ref

        if ">" in target_str:
            # merge src>tgt
            parts = target_str.split(">", 1)
            src = _resolve_merge_ref(parts[0])
            tgt = _resolve_merge_ref(parts[1])
        elif " into " in target_lower:
            # merge src into tgt
            idx = target_lower.index(" into ")
            src = _resolve_merge_ref(target_str[:idx])
            tgt = _resolve_merge_ref(target_str[idx + 6:])
        else:
            # merge <tgt> — use last pushed, session branch, or current branch as src
            tgt = _resolve_merge_ref(target_str)
            src = (last_pushed.get(ch.id)
                   or (session["branch"] if session else None)
                   or current_branch(cwd))

        if not src:
            await ch.send("Usage: `merge <target>`, `merge src>tgt`, or `merge src into tgt`")
            return
        if src == tgt:
            await ch.send(f"❌ Source and target are the same branch: `{src}`")
            return
        await ch.send(f"⏳ Merging `{src}` → `{tgt}`...")
        merge_result = await merge_branch(src, tgt, cwd)
        await ch.send(merge_result)
        if merge_result.startswith("✅") and session and src == session.get("branch"):
            canonical = _canonical_repo(cwd)
            _end_session(ch.id, cwd)
            record_state(ch.id, canonical)
        else:
            record_state(ch.id, cwd)
        return

    # ── PR commands ───────────────────────────────────────────────────────
    if lower.startswith("pr "):
        target_str = lower[3:].strip()
        tgt = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(target_str, target_str)
        src = last_pushed.get(ch.id)
        if not src:
            await ch.send("No recently pushed branch. Push first.")
            return
        await ch.send(f"⏳ Creating PR `{src}` → `{tgt}`...")
        await ch.send(await create_pr(src, tgt, f"auto: {src}", cwd))
        return

    # ── Login commands ────────────────────────────────────────────────────
    # Accepts: "claude login", "cc login", "codex login", "cx login", "kimi login", "km login", "login both"
    _is_claude_login = lower in ("claude login", "cc login")
    _is_codex_login  = lower in ("codex login", "cx login", "openai login")
    _is_kimi_login   = lower in ("kimi login", "km login")
    _is_both_login   = lower == "login both"
    if _is_claude_login or _is_codex_login or _is_kimi_login or _is_both_login:
        if _login_lock.get(ch.id):
            await ch.send("⏳ A login is already in progress in this channel.")
            return
        _login_lock[ch.id] = True
        try:
            if _is_claude_login or _is_both_login:
                await login_claude(ch)
            if _is_codex_login or _is_both_login:
                await login_codex(ch)
            if _is_kimi_login:
                await login_kimi(ch)
        finally:
            _login_lock.pop(ch.id, None)
        return

    # ── Info commands ─────────────────────────────────────────────────────
    if lower == "restart":
        _RESTART_FLAG.write_text(str(ch.id))
        await ch.send("🔄 Restarting bot...")
        _restart_on_close = True
        try:
            await asyncio.wait_for(client.close(), timeout=10)
        except asyncio.TimeoutError:
            print("⚠️ Close timed out, forcing restart...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        return


    if lower == "help":
        await ensure_pinned_help(ch)
        await ch.send("Help is pinned at the top of the channel.")
        return

    if lower == "status":
        st = run_git(["git", "status", "--short"], cwd).stdout.strip()
        br = current_branch(cwd)
        sess_info = ""
        if session:
            sess_runtime = get_session_runtime_config(session, ch.id)
            sess_model = get_model_for_engine(session["engine"], runtime_config=sess_runtime, ch_id=ch.id)
            sess_info = (
                f"\n📝 Active session: **{session['engine']}** (`{sess_model}`) · "
                f"{session['turns']} turn(s)"
            )
            sess_usage = session.get("total_usage", {})
            in_tok = sess_usage.get("input_tokens", 0)
            out_tok = sess_usage.get("output_tokens", 0)
            if in_tok or out_tok:
                sess_info += f" · {in_tok:,} in / {out_tok:,} out tokens"
            codex_thread_id = _clean_codex_thread_id(session.get("codex_thread_id"))
            if codex_thread_id:
                sess_info += f" · thread `{codex_thread_id}`"
        else:
            snapshot = load_unfinished_task_snapshot(ch.id)
            if snapshot:
                saved_engine = _normalize_engine_name(snapshot.get("engine"))
                saved_model = str(snapshot.get("model") or "?").strip() or "?"
                saved_branch = str(snapshot.get("branch") or "?").strip() or "?"
                sess_info = (
                    f"\n🧩 Saved unfinished session: **{saved_engine}** (`{saved_model}`) · "
                    f"`{saved_branch}` · use `resume`"
                )
                codex_thread_id = _clean_codex_thread_id(snapshot.get("codex_thread_id"))
                if codex_thread_id:
                    sess_info += f" · thread `{codex_thread_id}`"
        await ch.send(f"📍 `{cwd}`\n🌿 `{br}`{sess_info}\n"
                       f"```\n{st or '(clean)'}\n```")
        return

    if lower == "doctor":
        ssh_ok = check_github_ssh()
        claude_ok, claude_status = check_claude_cli()
        codex_ok, codex_status = check_codex_cli()
        kimi_ok, kimi_status = check_kimi_cli()
        codex_trusted = _load_codex_trusted_dirs()

        await ch.send(
            "🩺 **Diagnostics**\n"
            f"GitHub SSH: {'✅ OK' if ssh_ok else '⚠️ FAILED'}\n"
            f"Claude CLI: {'✅' if claude_ok else '⚠️'} {claude_status}\n"
            f"Codex CLI: {'✅' if codex_ok else '⚠️'} {codex_status}\n"
            f"Kimi CLI: {'✅' if kimi_ok else '⚠️'} {kimi_status}"
        )

        project_lines = []
        for idx, (label, path) in enumerate(GIT_PROJECTS, 1):
            active = " · active" if path == cwd else ""
            p = pathlib.Path(path)
            if not p.exists():
                project_lines.append(f"{idx}. {label}{active}: ⚠️ missing directory\n   `{path}`")
                continue
            if run_git(["git", "rev-parse", "--git-dir"], path).returncode != 0:
                project_lines.append(f"{idx}. {label}{active}: ⚠️ not a git repo\n   `{path}`")
                continue
            branch = current_branch(path) or "?"
            claude_tag = "trusted" if _is_claude_trusted(path) else "NOT trusted"
            codex_tag = "trusted" if _normalize_path(path) in codex_trusted else "NOT trusted"
            project_lines.append(
                f"{idx}. {label}{active}: `{branch}` · Claude {claude_tag} · Codex {codex_tag}\n"
                f"   `{path}`"
            )

        if project_lines:
            await ch.send(truncate("**Projects:**\n" + "\n".join(project_lines), 1800))

        fix_lines = []
        if not ssh_ok:
            fix_lines.append("GitHub SSH failed: run `ssh -T git@github.com` and load your SSH key.")
        if not claude_ok:
            fix_lines.append("Claude CLI unavailable: install/login Claude Code.")
        if not codex_ok:
            fix_lines.append("Codex CLI unavailable: install/login Codex CLI.")
        if not kimi_ok:
            fix_lines.append("Kimi CLI unavailable: install/login Kimi Code CLI.")
        if claude_ok:
            claude_untrusted = [path for _, path in GIT_PROJECTS if not _is_claude_trusted(path)]
            if claude_untrusted:
                fix_lines.append("Claude trust: run `claude` once interactively in each untrusted project directory.")
        if codex_ok:
            codex_untrusted = [path for _, path in GIT_PROJECTS if _normalize_path(path) not in codex_trusted]
            if codex_untrusted:
                fix_lines.append("Codex trust: run `codex` once interactively in each untrusted project directory.")
        if fix_lines:
            await ch.send("**Suggested fixes:**\n" + "\n".join(f"• {line}" for line in fix_lines))
        return

    if lower == "usage":
        stats = get_global_usage_stats()
        lines = ["📊 **Engine usage (all time)**"]
        if not stats:
            lines.append("No usage recorded yet.")
        else:
            for eng, data in sorted(stats.items()):
                in_tok = data.get("input_tokens", 0)
                out_tok = data.get("output_tokens", 0)
                cache_r = data.get("cache_read", 0)
                cache_w = data.get("cache_write", 0)
                runs = data.get("runs", 0)
                line = f"**{eng}**: {runs} run(s) · {in_tok:,} in / {out_tok:,} out"
                if cache_r or cache_w:
                    line += f" · {cache_r:,} cache read / {cache_w:,} cache write"
                lines.append(line)

        lines.append("\n📉 **Current remaining limits**")
        claude_ok, _ = check_claude_cli()
        codex_ok, _ = check_codex_cli()
        limit_tasks = []
        task_meta: list[str] = []
        if claude_ok:
            limit_tasks.append(asyncio.to_thread(get_claude_remaining_limit_summary))
            task_meta.append("claude")
        if codex_ok:
            limit_tasks.append(asyncio.to_thread(get_codex_remaining_limit_summary))
            task_meta.append("codex")

        live_limits: dict[str, tuple[str | None, str | None]] = {}
        if limit_tasks:
            results = await asyncio.gather(*limit_tasks)
            for eng, res in zip(task_meta, results):
                live_limits[eng] = res

        for eng in ("claude", "codex"):
            if eng not in live_limits:
                lines.append(f"**{eng}**: unavailable (CLI not installed or not authenticated)")
                continue
            summary, err = live_limits[eng]
            if summary:
                lines.append(f"**{eng}**: {summary}")
            else:
                lines.append(f"**{eng}**: unavailable ({err or 'unknown error'})")

        if session:
            sess_usage = session.get("total_usage", {})
            in_tok = sess_usage.get("input_tokens", 0)
            out_tok = sess_usage.get("output_tokens", 0)
            if in_tok or out_tok:
                lines.append(f"\n📝 **Current session**: {in_tok:,} in / {out_tok:,} out tokens "
                              f"({session['turns']} turn(s))")
        await ch.send("\n".join(lines))
        return

    if lower in ("context clear", "resume clear", "clear context"):
        cleared = clear_resume_context(ch.id)
        unfinished = clear_unfinished_task_snapshot(ch.id)
        queued = pop_queued_run_commands(ch.id)
        if cleared or unfinished or queued:
            parts = []
            if cleared:
                parts.append("saved resume context")
            if unfinished:
                parts.append("saved unfinished task snapshot")
            if queued:
                parts.append(f"{len(queued)} queued follow-up(s)")
            await ch.send(f"🧹 Cleared {' and '.join(parts)} for this channel.")
        else:
            await ch.send("No saved resume context, unfinished task snapshot, or queued follow-ups to clear.")
        return

    if lower == "plan show":
        plan_ctx = load_plan_context(ch.id)
        if not plan_ctx:
            await ch.send("No saved plan context for this channel. Run `plan: <task>` first.")
            return
        ts = plan_ctx.get("ts")
        if isinstance(ts, (int, float)):
            saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        else:
            saved_at = "unknown"
        plan_engine = (plan_ctx.get("engine") or "?").strip() or "?"
        plan_model = (plan_ctx.get("model") or "?").strip() or "?"
        plan_repo = (plan_ctx.get("cwd") or "").strip() or "(none)"
        plan_branch = (plan_ctx.get("branch") or "?").strip() or "?"
        plan_request = (plan_ctx.get("request") or "").strip() or "(empty)"
        plan_body = (plan_ctx.get("plan") or "").strip() or "(empty)"
        await ch.send(
            "🗂️ **Saved plan context**\n"
            f"Saved: `{saved_at}`\n"
            f"Engine: `{plan_engine}` · Model: `{plan_model}`\n"
            f"Repo: `{plan_repo}`\n"
            f"Branch: `{plan_branch}`\n"
            f"Request: {truncate(plan_request, 300)}"
        )
        await ch.send(f"```\n{truncate(plan_body, 1800)}\n```")
        return

    if lower in ("plan clear", "clear plan"):
        if clear_plan_context(ch.id):
            await ch.send("🧹 Cleared saved plan context.")
        else:
            await ch.send("No saved plan context to clear.")
        return

    if lower.startswith("plan:") or lower == "plan do" or lower.startswith("plan do ") or lower.startswith("plan do:"):
        if lower.startswith("plan:"):
            plan_input = content.split(":", 1)[1].strip()
        else:
            do_tail = content[len("plan do"):].strip()
            if do_tail.startswith(":"):
                do_tail = do_tail[1:].strip()
            plan_input = f"do {do_tail}".strip()
        if not plan_input:
            await ch.send(
                "Usage: `plan: <task>` or `plan: do [extra instructions]` (alias: `plan do [extra instructions]`)"
            )
            return

        plan_input_lower = plan_input.lower()
        execute_saved_plan = (
            plan_input_lower == "do"
            or plan_input_lower.startswith("do:")
            or plan_input_lower.startswith("do ")
        )

        if execute_saved_plan:
            plan_ctx = load_plan_context(ch.id)
            if not plan_ctx:
                await ch.send("No saved plan context for this channel. Run `plan: <task>` first.")
                return

            do_request = plan_input[2:].strip()
            if do_request.startswith(":"):
                do_request = do_request[1:].strip()
            runtime_config = get_runtime_config(ch.id)
            engine = get_default_engine(ch.id)
            label = get_engine_label(engine)
            model = get_model_for_engine(engine, runtime_config=runtime_config, ch_id=ch.id)
            plan_cwd = (plan_ctx.get("cwd") or "").strip() or cwd
            plan_branch = (plan_ctx.get("branch") or "").strip()
            if plan_branch == "?":
                plan_branch = ""
            plan_branch_exists = False
            exec_description = (
                do_request
                or (plan_ctx.get("request") or "").strip()
                or "execute saved plan"
            )
            execution_task = build_do_prompt(plan_ctx, do_request)
            clear_saved_plan = False

            try:
                if not plan_cwd or not pathlib.Path(plan_cwd).exists():
                    await ch.send(f"❌ Saved plan repo not found: `{plan_cwd or '(empty)'}`")
                    return
                plan_branch_exists = bool(
                    plan_branch
                    and (_branch_exists(plan_branch, plan_cwd) or _remote_branch_exists(plan_branch, plan_cwd))
                )

                if session:
                    await discard_changes(session["branch"], cwd)
                    _end_session(ch.id, cwd)
                    await ch.send("⚠️ Previous session discarded before executing saved plan.")
                    session = None

                cwd = plan_cwd
                channel_cwd[ch.id] = cwd
                await ch.send(
                    f"🚀 Executing saved plan with **{label}** (`{model}`) on `{cwd}`...\n"
                    f"> {truncate(exec_description, 200)}"
                )
                if plan_branch and not plan_branch_exists:
                    await ch.send(
                        f"⚠️ Saved planning branch `{plan_branch}` not found. "
                        "Using the default base branch instead."
                    )

                try:
                    cwd = ensure_worktree(cwd, ch.id)
                except Exception as e:
                    await ch.send(f"❌ Worktree creation failed: `{e}`")
                    return

                try:
                    branch = create_branch(
                        exec_description,
                        engine,
                        cwd,
                        base_branch=plan_branch if plan_branch_exists else None,
                        agent_id=ch.id,
                    )
                except Exception as e:
                    await ch.send(f"❌ Branch creation failed: `{e}`")
                    remove_worktree(_canonical_repo(cwd), ch.id)
                    channel_cwd[ch.id] = _canonical_repo(cwd)
                    return
                record_state(ch.id, cwd, branch)

                stop_event = asyncio.Event()
                stop_events[ch.id] = stop_event
                try:
                    output = await run_engine(
                        engine,
                        execution_task,
                        ch,
                        resume=False,
                        cwd=cwd,
                        stop_event=stop_event,
                        runtime_config=runtime_config,
                    )
                except Exception as e:
                    await ch.send(f"❌ {label} error: `{e}`")
                    await discard_changes(branch, cwd)
                    _end_session(ch.id, cwd)
                    return
                finally:
                    stop_events.pop(ch.id, None)

                if stop_event.is_set():
                    await discard_changes(branch, cwd)
                    canonical = _canonical_repo(cwd)
                    _end_session(ch.id, cwd)
                    # discard_changes can't delete the branch while the worktree has it
                    # checked out (git checkout main silently fails inside the worktree).
                    # Now that the worktree is gone, finish the deletion on the canonical repo.
                    if not is_protected_branch(branch):
                        run_git(["git", "branch", "-D", branch], canonical)
                    await ch.send(f"🗑️ Cleaned up branch `{branch}`.")
                    return

                clear_saved_plan = True

                try:
                    # If no files changed, clean up the branch and skip starting a session.
                    if not run_git(["git", "status", "--porcelain"], cwd).stdout.strip() and get_ahead_count(cwd) == 0:
                        canonical = _canonical_repo(cwd)
                        base = _resolve_checkout_branch(canonical, avoid=branch)
                        if base:
                            run_git(["git", "branch", "-D", branch], canonical)
                            record_state(ch.id, canonical, base)
                        else:
                            record_state(ch.id, canonical, current_branch(canonical) or branch)
                        remove_worktree(canonical, ch.id)
                        channel_cwd[ch.id] = canonical
                        await send_engine_output_block(ch, label, output)
                        msg = "ℹ️ No files changed — no session started."
                        if not base:
                            msg += " (Branch left checked out; no base branch found.)"
                        await ch.send(msg)
                        return

                    active_sessions[ch.id] = {
                        "branch": branch,
                        "engine": engine,
                        "description": exec_description,
                        "turns": 1,
                        "phase": "working",
                        "cwd": cwd,
                        "runtime_config": dict(runtime_config),
                        "total_usage": {},
                    }
                    _absorb_usage_into_session(active_sessions[ch.id], ch.id)

                    auto_commit(1, cwd)
                    await send_engine_output_block(ch, label, output)
                    stat = get_diff_stat(cwd)
                    await ch.send(f"📊 {stat}\n{_working_session_guidance(cwd)}")
                except Exception as exc:
                    logger.exception("Error in post-run handling for plan execution")
                    try:
                        await ch.send(f"⚠️ Plan execution finished but hit an error posting results: `{exc}`\n"
                                      "Your changes are on the branch — use `diff` or `review` to inspect.")
                    except discord.HTTPException:
                        pass
                return
            finally:
                if clear_saved_plan and clear_plan_context(ch.id):
                    await ch.send("🧹 Cleared saved plan context.")

        plan_request = plan_input
        runtime_config = get_runtime_config(ch.id)
        engine = get_default_engine(ch.id)
        label = get_engine_label(engine)
        model = get_model_for_engine(engine, runtime_config=runtime_config, ch_id=ch.id)
        planning_task = build_plan_prompt(plan_request, ch.id, cwd, engine)

        await ch.send(
            f"🧭 Planning with **{label}** (`{model}`) on `{cwd}`...\n"
            f"> {truncate(plan_request, 200)}"
        )
        stop_event = asyncio.Event()
        stop_events[ch.id] = stop_event
        try:
            output = await run_engine(
                engine,
                planning_task,
                ch,
                resume=False,
                cwd=cwd,
                stop_event=stop_event,
                runtime_config=runtime_config,
            )
        except Exception as e:
            await ch.send(f"❌ {label} planning error: `{e}`")
            return
        finally:
            stop_events.pop(ch.id, None)

        if stop_event.is_set():
            await ch.send("🛑 Planning stopped.")
            return

        save_plan_context(ch.id, cwd, engine, plan_request, output, runtime_config=runtime_config)
        await send_engine_output_block(
            ch,
            f"{label} Plan",
            output,
            failure_notice="⚠️ Planning finished, but I couldn't post the full plan output.",
        )
        await ch.send("💾 Saved plan context. Run `plan: do` (or `plan do`) to execute it.")
        return

    if lower == "branches":
        branches = get_branch_list(cwd)[:15]
        branch_listing[ch.id] = branches  # cache for N references
        cur = current_branch(cwd)
        listing = "\n".join(
            f"{'→' if b == cur else '•'} **{i}.** `{b}`"
            for i, b in enumerate(branches, 1)
        )
        await ch.send(f"**Recent branches ({cwd}):**\n{listing}\n\n"
                       f"_Use `N` in commands (e.g. `merge 1`, `branch switch 3`)_")
        return

    if lower.startswith("pull"):
        arg_raw = content[4:].strip()
        arg_lower = arg_raw.lower()
        if arg_raw:
            if arg_raw.lstrip("#").isdigit():
                # Numbered ref from the last `branches` listing (supports #N).
                branch = resolve_branch(arg_raw, ch.id, cwd)
                if branch is None:
                    await ch.send(
                        f"❌ `{arg_raw}` didn't match any branch. Run `branches` first to use `N` refs."
                    )
                    return
            else:
                branch = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(arg_lower, arg_raw)
        else:
            branch = _resolve_checkout_branch(cwd) or current_branch(cwd) or DEV_BRANCH
        if not branch:
            await ch.send("❌ No base branch found to pull.")
            return
        before_rev = _rev_parse_head(cwd)
        pull_timeout = GIT_NETWORK_TIMEOUT
        await ch.send(f"⏳ Pulling `{branch}` from remote...")
        try:
            fetch = run_git(["git", "fetch", "origin", branch], cwd, timeout=pull_timeout)
        except subprocess.TimeoutExpired:
            await ch.send(f"❌ Fetch timed out after {pull_timeout}s. Try again when the remote is responsive.")
            return
        if fetch.returncode != 0:
            error = truncate(
                _sanitize_code_block_text(fetch.stderr.strip() or fetch.stdout.strip() or "unknown error"),
                1600,
            )
            await ch.send(f"❌ Fetch failed:\n```\n{error}\n```")
            return
        try:
            pull = run_git(["git", "pull", "origin", branch], cwd, timeout=pull_timeout)
        except subprocess.TimeoutExpired:
            await ch.send(f"❌ Pull timed out after {pull_timeout}s. Try again or pull locally if it is still running.")
            return
        if pull.returncode != 0:
            error = truncate(
                _sanitize_code_block_text(pull.stderr.strip() or pull.stdout.strip() or "unknown error"),
                1600,
            )
            await ch.send(f"❌ Pull failed:\n```\n{error}\n```")
            return
        after_rev = _rev_parse_head(cwd)
        detail = (pull.stdout.strip() or pull.stderr.strip()).strip()
        already = (
            bool(before_rev and after_rev and before_rev == after_rev)
            or "already up to date" in detail.lower()
        )
        header = (
            f"✅ `{branch}` already up to date — nothing to pull."
            if already
            else f"✅ Pulled `{branch}` from remote."
        )
        if already:
            await ch.send(header)
            return

        lines = build_pull_change_summary_lines(before_rev, after_rev, cwd)
        if lines:
            await ch.send(header)
            await send_change_summary(ch, f"Pulled changes from `{branch}`", lines)
        elif detail:
            body = truncate(_sanitize_code_block_text(detail), 1600)
            await ch.send(f"{header}\n```\n{body}\n```")
        else:
            await ch.send(header)
        return

    # ── Multi-repo commands ───────────────────────────────────────────────
    if lower == "repos":
        lines = []
        for i, (label, path) in enumerate(GIT_PROJECTS, 1):
            branch = run_git_in(["git", "branch", "--show-current"], path).stdout.strip() or "?"
            st = run_git_in(["git", "status", "--porcelain"], path).stdout.strip()
            dirty = f" · {len(st.splitlines())} change(s)" if st else " · clean"
            active = " · active" if path == cwd else ""
            lines.append(f"**{i}. {label}** (`{branch}`){dirty}{active}\n   `{path}`")
        await ch.send("**Git projects:**\n" + "\n".join(lines))
        return

    if lower.startswith("repo "):
        parts = content[5:].strip().split(None, 1)  # use original case for commit msg
        if not parts:
            await ch.send("Usage: `repo <n> status|diff|review|commit [msg]|push|branches`")
            return
        proj = resolve_project(parts[0].lower())
        if proj is None:
            await ch.send(f"Project `{parts[0]}` not found. Use `repos` to list them.")
            return
        label, path = proj
        subcmd = parts[1].strip() if len(parts) > 1 else ""
        subcmd_lower = subcmd.lower()

        if subcmd_lower == "status":
            branch = run_git_in(["git", "branch", "--show-current"], path).stdout.strip()
            st = run_git_in(["git", "status", "--short"], path).stdout.strip()
            await ch.send(f"**{label}** · `{branch}`\n```\n{st or '(clean)'}\n```")

        elif subcmd_lower == "diff":
            diff = run_git_in(["git", "diff"], path).stdout.strip()
            staged = run_git_in(["git", "diff", "--cached"], path).stdout.strip()
            combined = (diff + "\n" + staged).strip() or "(no changes)"
            await ch.send(f"**{label} diff:**\n```diff\n{truncate(combined, 1800)}\n```")

        elif subcmd_lower == "review":
            entries = build_major_change_review(path)
            await send_major_change_review(ch, f"{label} major changes", entries)

        elif subcmd_lower.startswith("commit"):
            msg = subcmd[6:].strip() or "auto: bot commit"
            run_git_in(["git", "add", "."], path)
            st = run_git_in(["git", "status", "--porcelain"], path).stdout.strip()
            if not st:
                await ch.send(f"**{label}**: nothing to commit.")
                return
            result = run_git_in(["git", "commit", "-m", msg], path)
            if result.returncode == 0:
                await ch.send(f"✅ **{label}**: committed.\n```\n{result.stdout.strip()}\n```")
            else:
                await ch.send(f"❌ **{label}** commit failed:\n```\n{result.stderr.strip()}\n```")

        elif subcmd_lower == "push":
            branch = run_git_in(["git", "branch", "--show-current"], path).stdout.strip()
            result = run_git_in(["git", "push", "-u", "origin", branch], path)
            if result.returncode == 0:
                await ch.send(f"✅ **{label}**: pushed `{branch}`.")
            else:
                await ch.send(f"❌ **{label}** push failed:\n```\n{result.stderr.strip()}\n```")

        elif subcmd_lower == "branches":
            result = run_git_in(["git", "branch", "--sort=-committerdate",
                                  "--format=%(refname:short)"], path)
            branches = [b for b in result.stdout.strip().split("\n") if b][:15]
            current = run_git_in(["git", "branch", "--show-current"], path).stdout.strip()
            listing = "\n".join(f"{'→' if b == current else '•'} `{b}`" for b in branches)
            await ch.send(f"**{label} branches:**\n{listing or '(none)'}")

        else:
            await ch.send("Usage: `repo <n> status|diff|review|commit [msg]|push|branches`")
        return

    # ── Switch active working directory ───────────────────────────────────
    if lower.startswith("cwd"):
        arg = lower[3:].strip()
        if not arg:
            canonical = _canonical_repo(cwd)
            proj_label = next((l for l, p in GIT_PROJECTS if p == canonical), canonical)
            await ch.send(f"Active repo: **{proj_label}** (`{canonical}`)\nUse `cwd <n>` to switch.")
            return
        proj = resolve_project(arg)
        if proj is None:
            await ch.send(f"Project `{arg}` not found. Use `repos` to list them.")
            return
        label, path = proj
        if session:
            # Auto-commit current work before switching repos
            auto_commit(session["turns"], cwd)
            clear_unfinished_task_snapshot(ch.id)
            # Remove old worktree from previous repo
            remove_worktree(_canonical_repo(cwd), ch.id)
            # Create new worktree for the new repo
            try:
                new_wt = ensure_worktree(path, ch.id)
            except Exception as e:
                await ch.send(f"❌ Worktree creation failed for `{label}`: `{e}`")
                return
            new_branch = current_branch(new_wt)
            session["cwd"] = new_wt
            session["branch"] = new_branch
            channel_cwd[ch.id] = new_wt
            record_state(ch.id, new_wt, new_branch)
            await ch.send(f"✅ Switched to **{label}** (`{path}`) · branch `{new_branch}`")
        else:
            channel_cwd[ch.id] = path
            new_branch = current_branch(path)
            record_state(ch.id, path, new_branch)
            await ch.send(f"✅ Switched to **{label}** (`{path}`) · branch `{new_branch}`")
        return

    # ── Switch branch ─────────────────────────────────────────────────────
    if lower.startswith("branch switch ") or lower.startswith("switch "):
        prefix_len = len("branch switch ") if lower.startswith("branch switch ") else len("switch ")
        branch_ref = content[prefix_len:].strip()  # preserve case for branch name
        if not branch_ref:
            await ch.send("Usage: `branch switch <branch|N>`")
            return
        # Resolve N references
        branch_name = resolve_branch(branch_ref, ch.id, cwd) or branch_ref
        # Auto-commit current work before switching if we're mid-session
        if session:
            auto_commit(session["turns"], cwd)
            clear_unfinished_task_snapshot(ch.id)
        check = run_git(["git", "rev-parse", "--verify", branch_name], cwd)
        if check.returncode != 0:
            result = run_git(["git", "checkout", "-b", branch_name], cwd)
            action = "Created and switched to"
        else:
            result = run_git(["git", "checkout", branch_name], cwd)
            action = "Switched to"
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip() or "checkout failed"
            await ch.send(f"❌ Checkout failed:\n```\n{err}\n```")
            return
        await ch.send(f"🌿 {action} `{branch_name}`")
        if session:
            session["branch"] = branch_name
            record_state(ch.id, cwd, branch_name)
            stat = get_diff_stat(cwd)
            await ch.send(
                f"📊 {stat or 'clean'}\n{_working_session_guidance(cwd)}"
            )
        else:
            record_state(ch.id, cwd, branch_name)
        return

    # ── Branch delete ─────────────────────────────────────────────────────
    if lower.startswith("branch protect"):
        args = content[len("branch protect"):].strip().split()
        if not args or args[0].lower() == "list":
            listing = "\n".join(f"• `{b}`" for b in PROTECTED_BRANCHES) or "(none)"
            await ch.send(
                f"**Protected branches:**\n{listing}\n\n"
                "Use `branch protect add <branch>` or `branch protect remove <branch>`."
            )
            return

        action = args[0].lower()
        rest = args[1:]
        if action == "add":
            names = expand_branch_args(rest, ch.id, cwd)
            if not names:
                await ch.send("Usage: `branch protect add <branch...>`")
                return
            combined = PROTECTED_BRANCHES + names
            _set_protected_branches(combined)
            added = [b for b in names if is_protected_branch(b)]
            listing = "\n".join(f"• `{b}`" for b in added) or "(none)"
            await ch.send(f"✅ Added protected branches:\n{listing}")
            return

        if action in ("remove", "rm", "del", "delete"):
            names = expand_branch_args(rest, ch.id, cwd)
            if not names:
                await ch.send("Usage: `branch protect remove <branch...>`")
                return
            remove_keys = {b.lower() for b in names}
            remaining = [b for b in PROTECTED_BRANCHES if b.lower() not in remove_keys]
            _set_protected_branches(remaining)
            listing = "\n".join(f"• `{b}`" for b in names) or "(none)"
            await ch.send(f"✅ Removed protected branches:\n{listing}")
            return

        if action in ("clear",):
            _set_protected_branches([])
            await ch.send("✅ Cleared protected branches list.")
            return

        if action in ("reset", "default"):
            _set_protected_branches(_default_protected_branches())
            listing = "\n".join(f"• `{b}`" for b in PROTECTED_BRANCHES) or "(none)"
            await ch.send(f"✅ Reset protected branches:\n{listing}")
            return

        await ch.send("Usage: `branch protect [list|add|remove|clear|reset]`")
        return

    if lower.startswith("branch delete ") or lower.startswith("branch del "):
        prefix_len = len("branch delete ") if lower.startswith("branch delete ") else len("branch del ")
        parts = content[prefix_len:].strip().split()
        if not parts:
            await ch.send("Usage: `branch delete <name|N> [local|remote|both] [force]`")
            return
        branch_ref = parts[0]
        branch_name = resolve_branch(branch_ref, ch.id, cwd)
        if branch_name is None:
            await ch.send(f"❌ `{branch_ref}` didn't match any branch. Run `branches` first to use `N` refs.")
            return
        if is_protected_branch(branch_name):
            await ch.send(
                f"🛡️ `{branch_name}` is protected and cannot be deleted.\n"
                f"Use `branch protect remove {branch_name}` if you really need to delete it."
            )
            return
        flags = {p.lower() for p in parts[1:]}
        scope = "both"
        if "local" in flags:
            scope = "local"
        elif "remote" in flags:
            scope = "remote"
        force = "force" in flags

        # Don't delete currently checked-out branch
        if branch_name == current_branch(cwd):
            await ch.send(f"❌ `{branch_name}` is currently checked out. Switch branches first.")
            return

        # Check existence
        local_exists = run_git(["git", "rev-parse", "--verify", branch_name], cwd).returncode == 0
        remote_ref = run_git(["git", "ls-remote", "--heads", "origin", branch_name], cwd).stdout.strip()
        remote_exists = bool(remote_ref)

        if scope in ("local", "both") and not local_exists:
            await ch.send(f"⚠️ Local branch `{branch_name}` does not exist.")
            if scope == "local":
                return
        if scope in ("remote", "both") and not remote_exists:
            await ch.send(f"⚠️ Remote branch `origin/{branch_name}` does not exist.")
            if scope == "remote":
                return

        # Merged check
        if not force:
            run_git(["git", "fetch", "origin"], cwd)
            local_merged, remote_merged = branch_merged_status(branch_name, cwd)
            warnings = []
            if scope in ("local", "both") and local_exists and not local_merged:
                warnings.append("local branch has **unmerged commits**")
            if scope in ("remote", "both") and remote_exists and not remote_merged:
                warnings.append("remote branch has **unmerged commits**")
            if warnings:
                await ch.send(
                    f"⚠️ `{branch_name}`: {' and '.join(warnings)}.\n"
                    f"Add `force` to delete anyway: `branch delete {branch_name} {scope} force`"
                )
                return

        # Delete
        msgs = []
        if scope in ("local", "both") and local_exists:
            flag = "-D" if force else "-d"
            res = run_git(["git", "branch", flag, branch_name], cwd)
            if res.returncode == 0:
                msgs.append(f"✅ Local `{branch_name}` deleted.")
            else:
                msgs.append(f"❌ Local delete failed: {res.stderr.strip()}")
        if scope in ("remote", "both") and remote_exists:
            res = run_git(["git", "push", "origin", "--delete", branch_name], cwd)
            if res.returncode == 0:
                msgs.append(f"✅ Remote `origin/{branch_name}` deleted.")
            else:
                msgs.append(f"❌ Remote delete failed: {res.stderr.strip()}")
        await ch.send("\n".join(msgs))
        return

    engine_model_match = re.match(
        r"^engine(?:\s+(global|default))?\s+(claude|cc|codex|cx|openai|kimi|km)\s+model(?:\s+(.+))?$",
        content,
        flags=re.IGNORECASE,
    )
    if engine_model_match:
        scope_token = (engine_model_match.group(1) or "").lower()
        scope_ch_id = None if scope_token in ("global", "default") else ch.id
        engine_token = engine_model_match.group(2).lower()
        selector = (engine_model_match.group(3) or "").strip()
        target_engine = _engine_name_from_token(engine_token)
        if not selector:
            await ch.send(
                "Usage: `engine claude model <n|name> [reasoning <n|level>]`, "
                "`engine codex model <n|name> [reasoning <n|level>]`, "
                "`engine kimi model <n|name> [reasoning <n|level>]`, "
                "`engine global claude model <n|name> [reasoning <n|level>]`, "
                "`engine global codex model <n|name> [reasoning <n|level>]`, "
                "or `engine global kimi model <n|name> [reasoning <n|level>]`"
            )
            return
        model_selector, reasoning_selector, err = split_model_reasoning_selector(selector)
        if err:
            await ch.send(f"❌ {err}")
            return
        if target_engine == "claude":
            models = await get_claude_models()
            selected_model, err = resolve_model_selector(model_selector or "", models)
            if err:
                await ch.send(f"❌ {err}")
                return
            updates: dict[str, str | None] = {
                "default_engine": "claude",
                "claude_model": selected_model,
            }
            if reasoning_selector is not None:
                selected_effort, err = resolve_reasoning_selector(reasoning_selector, "claude")
                if err:
                    await ch.send(f"❌ {err}")
                    return
                updates["claude_reasoning_effort"] = selected_effort
            updated = update_runtime_config(scope_ch_id, **updates)
            details = f"model `{updated['claude_model']}`"
            if reasoning_selector is not None:
                details += f" · reasoning `{format_reasoning_effort(updated['claude_reasoning_effort'])}`"
            await ch.send(
                f"✅ {runtime_scope_name(scope_ch_id).capitalize()} default engine set to **claude** — "
                f"{details}"
            )
            return
        if target_engine == "kimi":
            models = get_kimi_models()
            selected_model, err = resolve_model_selector(model_selector or "", models)
            if err:
                await ch.send(f"❌ {err}")
                return
            updates: dict[str, str | None] = {
                "default_engine": "kimi",
                "kimi_model": selected_model,
            }
            if reasoning_selector is not None:
                selected_effort, err = resolve_reasoning_selector(reasoning_selector, "kimi")
                if err:
                    await ch.send(f"❌ {err}")
                    return
                updates["kimi_reasoning_effort"] = selected_effort
            updated = update_runtime_config(scope_ch_id, **updates)
            details = f"model `{updated['kimi_model']}`"
            if reasoning_selector is not None:
                details += f" · reasoning `{format_reasoning_effort(updated['kimi_reasoning_effort'])}`"
            await ch.send(
                f"✅ {runtime_scope_name(scope_ch_id).capitalize()} default engine set to **kimi** — "
                f"{details}"
            )
            return
        models = get_codex_models()
        selected_model, err = resolve_model_selector(model_selector or "", models)
        if err:
            await ch.send(f"❌ {err}")
            return
        updates: dict[str, str | None] = {
            "default_engine": "codex",
            "codex_model": selected_model,
        }
        if reasoning_selector is not None:
            selected_effort, err = resolve_reasoning_selector(reasoning_selector, "codex")
            if err:
                await ch.send(f"❌ {err}")
                return
            updates["codex_reasoning_effort"] = selected_effort
        updated = update_runtime_config(scope_ch_id, **updates)
        details = f"model `{updated['codex_model']}`"
        if reasoning_selector is not None:
            details += f" · reasoning `{format_reasoning_effort(updated['codex_reasoning_effort'])}`"
        await ch.send(
            f"✅ {runtime_scope_name(scope_ch_id).capitalize()} default engine set to **codex** — "
            f"{details}"
        )
        return

    engine_reasoning_match = re.match(
        r"^engine(?:\s+(global|default))?\s+(claude|cc|codex|cx|openai|kimi|km)\s+reasoning(?:\s+(.+))?$",
        content,
        flags=re.IGNORECASE,
    )
    if engine_reasoning_match:
        scope_token = (engine_reasoning_match.group(1) or "").lower()
        scope_ch_id = None if scope_token in ("global", "default") else ch.id
        engine_token = engine_reasoning_match.group(2).lower()
        selector = (engine_reasoning_match.group(3) or "").strip()
        target_engine = _engine_name_from_token(engine_token)
        if not selector:
            await ch.send(
                "Usage: `engine claude reasoning <n|level>`, `engine codex reasoning <n|level>`, "
                "`engine kimi reasoning <n|level>`, `engine global claude reasoning <n|level>`, "
                "`engine global codex reasoning <n|level>`, or `engine global kimi reasoning <n|level>`"
            )
            return
        selected_effort, err = resolve_reasoning_selector(selector, target_engine)
        if err:
            await ch.send(f"❌ {err}")
            return
        if target_engine == "claude":
            updated = update_runtime_config(
                scope_ch_id,
                default_engine="claude",
                claude_reasoning_effort=selected_effort,
            )
            await ch.send(
                f"✅ {runtime_scope_name(scope_ch_id).capitalize()} default engine set to **claude** — "
                f"reasoning `{format_reasoning_effort(updated['claude_reasoning_effort'])}`"
            )
            return
        if target_engine == "kimi":
            updated = update_runtime_config(
                scope_ch_id,
                default_engine="kimi",
                kimi_reasoning_effort=selected_effort,
            )
            await ch.send(
                f"✅ {runtime_scope_name(scope_ch_id).capitalize()} default engine set to **kimi** — "
                f"reasoning `{format_reasoning_effort(updated['kimi_reasoning_effort'])}`"
            )
            return
        updated = update_runtime_config(
            scope_ch_id,
            default_engine="codex",
            codex_reasoning_effort=selected_effort,
        )
        await ch.send(
            f"✅ {runtime_scope_name(scope_ch_id).capitalize()} default engine set to **codex** — "
            f"reasoning `{format_reasoning_effort(updated['codex_reasoning_effort'])}`"
        )
        return

    engine_only_match = re.match(
        r"^engine(?:\s+(global|default))?\s+(claude|cc|codex|cx|openai|kimi|km)$",
        content,
        flags=re.IGNORECASE,
    )
    if engine_only_match:
        scope_token = (engine_only_match.group(1) or "").lower()
        scope_ch_id = None if scope_token in ("global", "default") else ch.id
        engine_token = engine_only_match.group(2).lower()
        target_engine = _engine_name_from_token(engine_token)
        updated = update_runtime_config(scope_ch_id, default_engine=target_engine)
        model = get_model_for_engine(target_engine, runtime_config=updated)
        effort = get_reasoning_for_engine(target_engine, runtime_config=updated)
        await ch.send(
            f"✅ {runtime_scope_name(scope_ch_id).capitalize()} default engine set to **{target_engine}** — "
            f"current model `{model}` · reasoning `{format_reasoning_effort(effort)}`"
        )
        return

    if lower == "engine" or lower == "engine global" or lower == "engine default":
        show_global = lower in ("engine global", "engine default")
        channel_config = get_runtime_config(ch.id)
        global_config = get_runtime_config(None)
        config = global_config if show_global else channel_config
        claude_models = await get_claude_models()
        codex_models = get_codex_models()
        kimi_models = get_kimi_models()
        claude_list = " · ".join(f"`{mid}`" for mid, _ in claude_models)
        codex_list = " · ".join(f"`{slug}`" for slug, _ in codex_models)
        kimi_list = " · ".join(f"`{alias}`" for alias, _ in kimi_models)

        if show_global:
            body = (
                f"Global default: **{config['default_engine']}**\n"
                f"Claude: model `{config['claude_model']}` · reasoning "
                f"`{format_reasoning_effort(config['claude_reasoning_effort'])}`\n"
                f"Codex: model `{config['codex_model']}` · reasoning "
                f"`{format_reasoning_effort(config['codex_reasoning_effort'])}`\n"
                f"Kimi: model `{config['kimi_model']}` · reasoning "
                f"`{format_reasoning_effort(config['kimi_reasoning_effort'])}`\n\n"
            )
        else:
            scope_note = (
                "override active" if ch.id in CHANNEL_RUNTIME_CONFIGS else "inherits global default"
            )
            body = (
                f"This channel ({scope_note}): **{config['default_engine']}**\n"
                f"Claude: model `{config['claude_model']}` · reasoning "
                f"`{format_reasoning_effort(config['claude_reasoning_effort'])}`\n"
                f"Codex: model `{config['codex_model']}` · reasoning "
                f"`{format_reasoning_effort(config['codex_reasoning_effort'])}`\n"
                f"Kimi: model `{config['kimi_model']}` · reasoning "
                f"`{format_reasoning_effort(config['kimi_reasoning_effort'])}`\n\n"
                f"Global default: **{global_config['default_engine']}**\n"
                f"Claude: model `{global_config['claude_model']}` · reasoning "
                f"`{format_reasoning_effort(global_config['claude_reasoning_effort'])}`\n"
                f"Codex: model `{global_config['codex_model']}` · reasoning "
                f"`{format_reasoning_effort(global_config['codex_reasoning_effort'])}`\n"
                f"Kimi: model `{global_config['kimi_model']}` · reasoning "
                f"`{format_reasoning_effort(global_config['kimi_reasoning_effort'])}`\n\n"
            )

        await ch.send(
            body
            + f"**Available models:**\n"
            + f"Claude: {claude_list}\n"
            + f"Codex: {codex_list}\n"
            + f"Kimi: {kimi_list}\n\n"
            + f"**Reasoning levels (name or number):**\n"
            + f"Claude:\n{format_reasoning_options_numbered('claude')}\n"
            + f"Codex:\n{format_reasoning_options_numbered('codex')}\n"
            + f"Kimi:\n{format_reasoning_options_numbered('kimi')}"
        )
        return

    if lower.startswith("engine "):
        await ch.send(
            "Usage: `engine`, `engine global`, `engine claude`, `engine codex`, `engine kimi`, "
            "`engine claude model <n|name> [reasoning <n|level>]`, "
            "`engine codex model <n|name> [reasoning <n|level>]`, "
            "`engine kimi model <n|name> [reasoning <n|level>]`, "
            "`engine claude reasoning <n|level>`, `engine codex reasoning <n|level>`, "
            "`engine kimi reasoning <n|level>`, "
            "`engine global claude|codex|kimi`, "
            "`engine global claude|codex|kimi model <n|name> [reasoning <n|level>]`, "
            "or `engine global claude|codex|kimi reasoning <n|level>`"
        )
        return

    # ── Model listing ────────────────────────────────────────────────────
    # Accepts: "claude models", "cc models", "codex models", "cx models", "kimi models", "km models"
    if lower in ("claude models", "cc models"):
        channel_config = get_runtime_config(ch.id)
        current_model = get_model_for_engine("claude", runtime_config=channel_config)
        global_model = get_model_for_engine("claude")
        models = await get_claude_models()
        listing = "\n".join(
            f"{idx}. {'▶ ' if mid == current_model else ''}`{mid}`"
            + (f" ({display_name})" if display_name != mid else "")
            for idx, (mid, display_name) in enumerate(models, start=1)
        )
        await ch.send(
            f"**Claude models** (this channel: `{current_model}` · global default: `{global_model}`):\n"
            f"{listing}\n\n"
            "Switch this channel with `claude model <n|name>` or "
            "`engine claude model <n|name> [reasoning <n|level>]`. "
            "Switch global default with `engine global claude model <n|name> [reasoning <n|level>]`."
        )
        return
    if lower in ("codex models", "cx models"):
        channel_config = get_runtime_config(ch.id)
        current_model = get_model_for_engine("codex", runtime_config=channel_config)
        global_model = get_model_for_engine("codex")
        models = get_codex_models()
        def _ctx_label(ctx: int | None) -> str:
            if ctx is None:
                return ""
            return f" ({ctx // 1000}K ctx)"
        listing = "\n".join(
            f"{idx}. {'▶ ' if slug == current_model else ''}`{slug}`{_ctx_label(ctx)}"
            for idx, (slug, ctx) in enumerate(models, start=1)
        )
        await ch.send(
            f"**Codex models** (this channel: `{current_model}` · global default: `{global_model}`):\n"
            f"{listing}\n\n"
            "Switch this channel with `codex model <n|name>` or "
            "`engine codex model <n|name> [reasoning <n|level>]`. "
            "Switch global default with `engine global codex model <n|name> [reasoning <n|level>]`."
        )
        return
    if lower in ("kimi models", "km models"):
        channel_config = get_runtime_config(ch.id)
        current_model = get_model_for_engine("kimi", runtime_config=channel_config)
        global_model = get_model_for_engine("kimi")
        models = get_kimi_models()
        listing = "\n".join(
            f"{idx}. {'▶ ' if alias == current_model else ''}`{alias}`"
            + (f" ({display_name})" if display_name and display_name != alias else "")
            for idx, (alias, display_name) in enumerate(models, start=1)
        )
        await ch.send(
            f"**Kimi models** (this channel: `{current_model}` · global default: `{global_model}`):\n"
            f"{listing}\n\n"
            "Switch this channel with `kimi model <n|name>` or "
            "`engine kimi model <n|name> [reasoning <n|level>]`. "
            "Switch global default with `engine global kimi model <n|name> [reasoning <n|level>]`."
        )
        return

    # ── Model change ─────────────────────────────────────────────────────
    # Accepts: "claude model <n|name>", "cc model <n|name>", "codex model <n|name>", "cx model <n|name>",
    #          "kimi model <n|name>", "km model <n|name>"
    _model_prefixes = {
        "claude model ": "claude", "cc model ": "claude",
        "codex model ": "codex",   "cx model ": "codex",
        "kimi model ": "kimi",     "km model ": "kimi",
    }
    for _pfx, _engine in _model_prefixes.items():
        if lower.startswith(_pfx):
            selector = content[len(_pfx):].strip()
            channel_config = get_runtime_config(ch.id)
            if not selector:
                await ch.send(
                    f"Usage: `{_pfx.strip()} <n|name>`\n"
                    f"This channel — Claude: `{channel_config['claude_model']}` · "
                    f"Codex: `{channel_config['codex_model']}` · "
                    f"Kimi: `{channel_config['kimi_model']}`"
                )
                return
            if _engine == "claude":
                models = await get_claude_models()
                selected_model, err = resolve_model_selector(selector, models)
                if err:
                    await ch.send(f"❌ {err}")
                    return
                updated = update_runtime_config(ch.id, claude_model=selected_model)
                await ch.send(f"✅ This channel Claude model set to `{updated['claude_model']}`")
            elif _engine == "kimi":
                models = get_kimi_models()
                selected_model, err = resolve_model_selector(selector, models)
                if err:
                    await ch.send(f"❌ {err}")
                    return
                updated = update_runtime_config(ch.id, kimi_model=selected_model)
                await ch.send(f"✅ This channel Kimi model set to `{updated['kimi_model']}`")
            else:
                models = get_codex_models()
                selected_model, err = resolve_model_selector(selector, models)
                if err:
                    await ch.send(f"❌ {err}")
                    return
                updated = update_runtime_config(ch.id, codex_model=selected_model)
                await ch.send(f"✅ This channel Codex model set to `{updated['codex_model']}`")
            return

    # ── Reasoning change (engine-specific) ─────────────────────────────
    # Accepts: "claude reasoning [n|level]", "cc reasoning [n|level]",
    #          "codex reasoning [n|level]", "cx reasoning [n|level]", "openai reasoning [n|level]",
    #          "kimi reasoning [n|level]", "km reasoning [n|level]"
    reasoning_match = re.match(
        r"^(claude|cc|codex|cx|openai|kimi|km)\s+reasoning(?:\s+(.+))?$",
        content,
        flags=re.IGNORECASE,
    )
    if reasoning_match:
        engine_token = reasoning_match.group(1).lower()
        selector = (reasoning_match.group(2) or "").strip()
        target_engine = _engine_name_from_token(engine_token)
        channel_config = get_runtime_config(ch.id)
        if not selector:
            if target_engine == "claude":
                await ch.send(
                    f"Claude reasoning (this channel): "
                    f"`{format_reasoning_effort(channel_config['claude_reasoning_effort'])}`\n"
                    f"Levels:\n{format_reasoning_options_numbered('claude')}\n"
                    "Set with `claude reasoning <n|level>`"
                )
            elif target_engine == "kimi":
                await ch.send(
                    f"Kimi reasoning (this channel): "
                    f"`{format_reasoning_effort(channel_config['kimi_reasoning_effort'])}`\n"
                    f"Levels:\n{format_reasoning_options_numbered('kimi')}\n"
                    "Set with `kimi reasoning <n|level>`"
                )
            else:
                await ch.send(
                    f"Codex reasoning (this channel): "
                    f"`{format_reasoning_effort(channel_config['codex_reasoning_effort'])}`\n"
                    f"Levels:\n{format_reasoning_options_numbered('codex')}\n"
                    "Set with `codex reasoning <n|level>`"
                )
            return
        selected_effort, err = resolve_reasoning_selector(selector, target_engine)
        if err:
            await ch.send(f"❌ {err}")
            return
        if target_engine == "claude":
            updated = update_runtime_config(ch.id, claude_reasoning_effort=selected_effort)
            await ch.send(
                "✅ This channel Claude reasoning set to "
                f"`{format_reasoning_effort(updated['claude_reasoning_effort'])}`"
            )
            return
        if target_engine == "kimi":
            updated = update_runtime_config(ch.id, kimi_reasoning_effort=selected_effort)
            await ch.send(
                "✅ This channel Kimi reasoning set to "
                f"`{format_reasoning_effort(updated['kimi_reasoning_effort'])}`"
            )
            return
        updated = update_runtime_config(ch.id, codex_reasoning_effort=selected_effort)
        await ch.send(
            "✅ This channel Codex reasoning set to "
            f"`{format_reasoning_effort(updated['codex_reasoning_effort'])}`"
        )
        return

    # ── Default model change ────────────────────────────────────────────
    # Accepts: "model <n|name>", "default model <n|name>"
    if lower == "model" or lower.startswith("model ") or lower == "default model" or lower.startswith("default model "):
        if lower.startswith("default model"):
            prefix = "default model"
        else:
            prefix = "model"
        new_model = content[len(prefix):].strip()
        channel_config = get_runtime_config(ch.id)
        default_engine = _normalize_engine_name(channel_config.get("default_engine"))
        if not new_model:
            if default_engine == "claude":
                current = str(channel_config["claude_model"])
                current_reasoning = format_reasoning_effort(channel_config["claude_reasoning_effort"])
            elif default_engine == "codex":
                current = str(channel_config["codex_model"])
                current_reasoning = format_reasoning_effort(channel_config["codex_reasoning_effort"])
            else:
                current = str(channel_config["kimi_model"])
                current_reasoning = format_reasoning_effort(channel_config["kimi_reasoning_effort"])
            await ch.send(
                f"Usage: `{prefix} <n|name>`\n"
                f"This channel default engine: `{default_engine}` · Current model: `{current}` · "
                f"Current reasoning: `{current_reasoning}`\n"
                "Use `claude model <n|name>` / `codex model <n|name>` / `kimi model <n|name>` to set explicitly."
            )
            return
        if default_engine == "claude":
            models = await get_claude_models()
            selected_model, err = resolve_model_selector(new_model, models)
            if err:
                await ch.send(f"❌ {err}")
                return
            updated = update_runtime_config(ch.id, claude_model=selected_model)
            await ch.send(
                "✅ This channel default engine is **claude** — "
                f"model set to `{updated['claude_model']}`"
            )
            return
        if default_engine == "codex":
            models = get_codex_models()
            selected_model, err = resolve_model_selector(new_model, models)
            if err:
                await ch.send(f"❌ {err}")
                return
            updated = update_runtime_config(ch.id, codex_model=selected_model)
            await ch.send(
                "✅ This channel default engine is **codex** — "
                f"model set to `{updated['codex_model']}`"
            )
            return
        models = get_kimi_models()
        selected_model, err = resolve_model_selector(new_model, models)
        if err:
            await ch.send(f"❌ {err}")
            return
        updated = update_runtime_config(ch.id, kimi_model=selected_model)
        await ch.send(
            "✅ This channel default engine is **kimi** — "
            f"model set to `{updated['kimi_model']}`"
        )
        return

    # ── Default reasoning change ────────────────────────────────────────
    # Accepts: "reasoning [n|level]", "default reasoning [n|level]"
    if lower == "reasoning" or lower.startswith("reasoning ") or lower == "default reasoning" or lower.startswith("default reasoning "):
        prefix = "default reasoning" if lower.startswith("default reasoning") else "reasoning"
        new_effort = content[len(prefix):].strip()
        channel_config = get_runtime_config(ch.id)
        default_engine = _normalize_engine_name(channel_config.get("default_engine"))
        if not new_effort:
            if default_engine == "claude":
                await ch.send(
                    f"Usage: `{prefix} <n|level>`\n"
                    f"This channel default engine: `claude` · Current reasoning: "
                    f"`{format_reasoning_effort(channel_config['claude_reasoning_effort'])}`\n"
                    f"Levels:\n{format_reasoning_options_numbered('claude')}"
                )
            elif default_engine == "codex":
                await ch.send(
                    f"Usage: `{prefix} <n|level>`\n"
                    f"This channel default engine: `codex` · Current reasoning: "
                    f"`{format_reasoning_effort(channel_config['codex_reasoning_effort'])}`\n"
                    f"Levels:\n{format_reasoning_options_numbered('codex')}"
                )
            else:
                await ch.send(
                    f"Usage: `{prefix} <n|level>`\n"
                    f"This channel default engine: `kimi` · Current reasoning: "
                    f"`{format_reasoning_effort(channel_config['kimi_reasoning_effort'])}`\n"
                    f"Levels:\n{format_reasoning_options_numbered('kimi')}"
                )
            return
        if default_engine == "claude":
            selected_effort, err = resolve_reasoning_selector(new_effort, "claude")
            if err:
                await ch.send(f"❌ {err}")
                return
            updated = update_runtime_config(ch.id, claude_reasoning_effort=selected_effort)
            await ch.send(
                "✅ This channel default engine is **claude** — reasoning set to "
                f"`{format_reasoning_effort(updated['claude_reasoning_effort'])}`"
            )
            return
        if default_engine == "codex":
            selected_effort, err = resolve_reasoning_selector(new_effort, "codex")
            if err:
                await ch.send(f"❌ {err}")
                return
            updated = update_runtime_config(ch.id, codex_reasoning_effort=selected_effort)
            await ch.send(
                "✅ This channel default engine is **codex** — reasoning set to "
                f"`{format_reasoning_effort(updated['codex_reasoning_effort'])}`"
            )
            return
        selected_effort, err = resolve_reasoning_selector(new_effort, "kimi")
        if err:
            await ch.send(f"❌ {err}")
            return
        updated = update_runtime_config(ch.id, kimi_reasoning_effort=selected_effort)
        await ch.send(
            "✅ This channel default engine is **kimi** — reasoning set to "
            f"`{format_reasoning_effort(updated['kimi_reasoning_effort'])}`"
        )
        return

    # ── Recover orphaned branches ────────────────────────────────────────
    if lower == "recover":
        canonical = _canonical_repo(cwd)
        result = run_git(["git", "branch", "--sort=-committerdate",
                          "--format=%(refname:short)"], canonical)
        orphans = [b for b in result.stdout.strip().split("\n")
                   if b.startswith(f"{BRANCH_PREFIX}/")]
        if not orphans:
            await ch.send("No orphaned feature branches found.")
            return
        listing = "\n".join(
            f"• `{b}` — recover with `recover {b.rsplit('-', 1)[-1]}`"
            for b in orphans[:10]
        )
        await ch.send(f"**Orphaned feature branches:**\n{listing}\n\n"
                       f"Use the short ID or full branch name.")
        return

    if lower.startswith("recover drop ") or lower.startswith("recover "):
        is_drop = lower.startswith("recover drop ")
        arg = content[13:].strip() if is_drop else content[8:].strip()
        canonical = _canonical_repo(cwd)

        # Resolve short ID (trailing digits) to full branch name
        if not arg.startswith(f"{BRANCH_PREFIX}/"):
            result = run_git(["git", "branch", "--sort=-committerdate",
                              "--format=%(refname:short)"], canonical)
            candidates = [b for b in result.stdout.strip().split("\n")
                          if b.startswith(f"{BRANCH_PREFIX}/") and b.endswith(f"-{arg}")]
            if len(candidates) == 1:
                arg = candidates[0]
            elif len(candidates) > 1:
                listing = "\n".join(f"• `{b}`" for b in candidates)
                await ch.send(f"Multiple matches for `{arg}`:\n{listing}\nUse the full name.")
                return
            else:
                await ch.send(f"No feature branch ending in `{arg}` found.")
                return

        if is_drop:
            if is_protected_branch(arg):
                await ch.send(
                    f"🛡️ `{arg}` is protected and cannot be deleted.\n"
                    f"Use `branch protect remove {arg}` if you really need to delete it."
                )
                return
            run_git(["git", "branch", "-D", arg], canonical)
            run_git(["git", "push", "origin", "--delete", arg], canonical)
            await ch.send(f"🗑️ Deleted `{arg}` locally and remotely.")
            return

        branch = arg
        # Check branch exists (use canonical repo for branch lookup)
        canonical = _canonical_repo(cwd)
        check = run_git(["git", "rev-parse", "--verify", branch], canonical)
        if check.returncode != 0:
            await ch.send(f"Branch `{branch}` not found.")
            return
        if session:
            await discard_changes(session["branch"], cwd)
            _end_session(ch.id, cwd)
            await ch.send("⚠️ Previous session discarded.\n")
        snapshot = load_unfinished_task_snapshot(ch.id)
        if snapshot and str(snapshot.get("branch") or "").strip() == branch:
            clear_unfinished_task_snapshot(ch.id)
        # Parse engine from branch name (auto/engine/slug-agent-nonce)
        parts = branch.split("/")
        engine = _normalize_engine_name(parts[1] if len(parts) >= 3 else get_default_engine(ch.id))
        try:
            restored = activate_session_on_branch(
                ch.id,
                canonical,
                branch,
                engine,
                "recovered session",
                runtime_config=get_runtime_config(ch.id),
                turns=0,
            )
        except Exception as e:
            await ch.send(f"❌ {e}")
            return
        diff_stat = get_diff_stat(restored["cwd"])
        await ch.send(
            f"♻️ Recovered session on `{branch}`\n📊 {diff_stat}\n{_working_session_guidance(restored['cwd'])}"
        )
        return

    # ── Follow-up in active session ───────────────────────────────────────
    if session and session.get("phase") != "review":
        engine = session["engine"]
        label = get_engine_label(engine)
        session_runtime_config = get_session_runtime_config(session, ch.id)
        session_model = get_model_for_engine(engine, runtime_config=session_runtime_config, ch_id=ch.id)
        session["turns"] += 1

        images = await download_attachments(message)
        follow_up_task = content or "Describe and analyze these images"
        _record_session_followup(session, follow_up_task)

        await ch.send(f"🔄 **{label}** (`{session_model}`) follow-up (turn {session['turns']})...\n"
                       f"> {truncate(follow_up_task, 200)}"
                       + (f"\n📎 {len(images)} image(s) attached" if images else ""))
        stop_event = asyncio.Event()
        stop_events[ch.id] = stop_event
        try:
            output = await run_engine(
                engine,
                follow_up_task,
                ch,
                resume=True,
                images=images,
                cwd=cwd,
                stop_event=stop_event,
                runtime_config=session_runtime_config,
            )
        except Exception as e:
            await ch.send(f"❌ Error: `{e}`")
            return
        finally:
            stop_events.pop(ch.id, None)

        if stop_event.is_set():
            return

        try:
            _absorb_usage_into_session(session, ch.id)
            auto_commit(session["turns"], cwd)
            await send_engine_output_block(ch, label, output)
            await send_working_session_wrapup(ch, session, cwd)
        except Exception as exc:
            logger.exception("Error in post-run handling for follow-up")
            try:
                await ch.send(f"⚠️ Follow-up finished but hit an error posting results: `{exc}`\n"
                              "Your changes are on the branch — use `diff` or `review` to inspect.")
            except discord.HTTPException:
                pass
        return

    # ── New task → start a session ────────────────────────────────────────
    images = await download_attachments(message)
    channel_runtime = get_runtime_config(ch.id)
    default_engine = get_default_engine(ch.id)
    engine, task = parse_engine_and_task(content, default_engine) if content else (default_engine, "")
    runtime_config = dict(channel_runtime)
    model = get_model_for_engine(engine, runtime_config=runtime_config, ch_id=ch.id)
    if not task and images:
        task = "Describe and analyze these images"
    if not task:
        await ch.send("Give me a task to work on.")
        return

    if not session:
        clear_unfinished_task_snapshot(ch.id)

    # Clean up any leftover session
    if session:
        await discard_changes(session["branch"], cwd)
        _end_session(ch.id, cwd)
        cwd = _canonical_repo(cwd)  # reset to canonical after cleanup
        await ch.send("⚠️ Previous session discarded.\n")

    label = get_engine_label(engine)
    await ch.send(f"🧠 **{label}** (`{model}`) starting on `{cwd}`...\n> {truncate(task, 200)}"
                   + (f"\n📎 {len(images)} image(s) attached" if images else ""))

    try:
        cwd = ensure_worktree(cwd, ch.id)
    except Exception as e:
        await ch.send(f"❌ Worktree creation failed: `{e}`")
        return

    try:
        branch = create_branch(task, engine, cwd, agent_id=ch.id)
    except Exception as e:
        await ch.send(f"❌ Branch creation failed: `{e}`")
        remove_worktree(_canonical_repo(cwd), ch.id)
        channel_cwd[ch.id] = _canonical_repo(cwd)
        return
    record_state(ch.id, cwd, branch)

    stop_event = asyncio.Event()
    stop_events[ch.id] = stop_event
    try:
        output = await run_engine(
            engine,
            task,
            ch,
            resume=False,
            images=images,
            cwd=cwd,
            stop_event=stop_event,
            runtime_config=runtime_config,
        )
    except Exception as e:
        await ch.send(f"❌ {label} error: `{e}`")
        await discard_changes(branch, cwd)
        _end_session(ch.id, cwd)
        return
    finally:
        stop_events.pop(ch.id, None)

    if stop_event.is_set():
        await discard_changes(branch, cwd)
        canonical = _canonical_repo(cwd)
        _end_session(ch.id, cwd)
        # discard_changes can't delete the branch while the worktree has it
        # checked out (git checkout main silently fails inside the worktree).
        # Now that the worktree is gone, finish the deletion on the canonical repo.
        if not is_protected_branch(branch):
            run_git(["git", "branch", "-D", branch], canonical)
        await ch.send(f"🗑️ Cleaned up branch `{branch}`.")
        return

    try:
        # If no files changed, clean up the branch and skip starting a session
        if not run_git(["git", "status", "--porcelain"], cwd).stdout.strip() and get_ahead_count(cwd) == 0:
            canonical = _canonical_repo(cwd)
            base = _resolve_checkout_branch(canonical, avoid=branch)
            if base:
                run_git(["git", "branch", "-D", branch], canonical)
                record_state(ch.id, canonical, base)
            else:
                record_state(ch.id, canonical, current_branch(canonical) or branch)
            remove_worktree(canonical, ch.id)
            channel_cwd[ch.id] = canonical
            await send_engine_output_block(ch, label, output)
            msg = "ℹ️ No files changed — no session started."
            if not base:
                msg += " (Branch left checked out; no base branch found.)"
            await ch.send(msg)
            return

        # Create session
        active_sessions[ch.id] = {
            "branch": branch,
            "engine": engine,
            "description": task,
            "turns": 1,
            "phase": "working",
            "cwd": cwd,
            "runtime_config": dict(runtime_config),
            "total_usage": {},
        }
        _absorb_usage_into_session(active_sessions[ch.id], ch.id)

        auto_commit(1, cwd)
        await send_engine_output_block(ch, label, output)
        await send_working_session_wrapup(ch, active_sessions[ch.id], cwd)
    except Exception as exc:
        logger.exception("Error in post-run handling for new task")
        try:
            await ch.send(f"⚠️ Task finished but hit an error posting results: `{exc}`\n"
                          "Your changes are on the branch — use `diff` or `review` to inspect.")
        except discord.HTTPException:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

async def stdin_listener():
    """Read stdin in a thread, close the bot when user types 'exit' or 'quit'."""
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:  # EOF (e.g. piped input ended)
            break
        cmd = line.strip().lower()
        if cmd in ("exit", "quit", "shutdown"):
            print("🛑 Shutting down...")
            await client.close()
            break


async def main():
    async with client:
        client.loop.create_task(stdin_listener())
        await client.start(DISCORD_TOKEN)
    if _restart_on_close:
        print("🔄 Restarting process...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        # Suppress "Event loop is closed" noise from subprocess transport __del__
        sys.stderr = open(os.devnull, "w")
