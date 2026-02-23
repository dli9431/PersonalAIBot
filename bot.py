#!/usr/bin/env python3
"""
Discord → Claude Code / Codex CLI → Git bridge bot.

Supports iterative sessions: send a task, review changes, send follow-ups,
and only commit when you're satisfied. Uses --resume (Claude Code) and
exec resume --last (Codex) for multi-turn context.

Designed to run on Linux (including WSL2).

Requirements:
    pip install discord.py python-dotenv
    Optional: gh CLI (for PR creation)
"""

import asyncio
import json
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

DEFAULT_ENGINE = os.getenv("DEFAULT_ENGINE", "claude")

# Claude Code (mutable at runtime via Discord `claude model` / `model` commands)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "sonnet")
CLAUDE_ALLOWED_TOOLS = os.getenv("CLAUDE_ALLOWED_TOOLS",
    "Read Edit Write Grep Glob LS Bash(git\\ diff) Bash(git\\ status)"
).split()
CLAUDE_DENIED_TOOLS = os.getenv("CLAUDE_DENIED_TOOLS",
    "Bash(rm\\ *) Bash(sudo\\ *) Bash(curl\\ *) Bash(wget\\ *) WebFetch"
).split()

# Codex CLI
CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.3-codex")

ENGINE_TIMEOUT = int(os.getenv("ENGINE_TIMEOUT", "300"))
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
_RESTART_FLAG = pathlib.Path("/tmp/bot_restart_channel")

# ── Discord client setup ─────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


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


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_authorised(msg: discord.Message) -> bool:
    return msg.author.id == ALLOWED_USER_ID


def slugify(text: str, max_len: int = 40) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in text.lower())
    return (slug.strip("-")[:max_len].rstrip("-")) or "task"


def run_git(cmd: list[str], path: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=path or REPO_PATH, capture_output=True, text=True, timeout=60,
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


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def truncate(text: str, limit: int = MAX_DIFF_CHARS) -> str:
    if len(text) <= limit:
        return text
    h = limit // 2 - 20
    return text[:h] + "\n\n... (truncated) ...\n\n" + text[-h:]


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
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError:
        pass


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


def _save_runtime_config() -> None:
    data = _load_state()
    data["runtime_config"] = {
        "default_engine": "codex" if (DEFAULT_ENGINE or "").strip().lower() == "codex" else "claude",
        "claude_model": CLAUDE_MODEL,
        "codex_model": CODEX_MODEL,
    }
    _save_state(data)


def _load_runtime_config() -> None:
    global DEFAULT_ENGINE, CLAUDE_MODEL, CODEX_MODEL
    data = _load_state()
    config = data.get("runtime_config")
    if not isinstance(config, dict):
        return
    default_engine = config.get("default_engine")
    if isinstance(default_engine, str) and default_engine.strip():
        DEFAULT_ENGINE = "codex" if default_engine.strip().lower() == "codex" else "claude"
    claude_model = config.get("claude_model")
    if isinstance(claude_model, str) and claude_model.strip():
        CLAUDE_MODEL = claude_model.strip()
    codex_model = config.get("codex_model")
    if isinstance(codex_model, str) and codex_model.strip():
        CODEX_MODEL = codex_model.strip()


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
        "branch": branch,
        "updated": int(time.time()),
    }
    data["last_active_channel"] = ch_id
    _save_state(data)


def restore_state() -> tuple[int | None, str | None, str | None, str | None]:
    data = _load_state()
    channels = data.get("channels", {}) or {}
    for ch_id_str, info in channels.items():
        if isinstance(info, dict):
            cwd = info.get("cwd")
            if cwd:
                try:
                    channel_cwd[int(ch_id_str)] = cwd
                except ValueError:
                    continue
    last_id = data.get("last_active_channel")
    if last_id is None:
        return None, None, None, None
    info = channels.get(str(last_id)) or {}
    cwd = info.get("cwd")
    branch = info.get("branch")
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


def _tail_text(text: str, max_chars: int = CONTEXT_MAX_CHARS) -> str:
    if not text:
        return ""
    clean = strip_ansi(text)
    if len(clean) <= max_chars:
        return clean
    return clean[-max_chars:]


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


def _safe_current_branch(path: str | None) -> str:
    if not path or not pathlib.Path(path).exists():
        return "?"
    try:
        return current_branch(path) or "?"
    except Exception:
        return "?"


def save_plan_context(
    ch_id: int,
    cwd: str | None,
    engine: str,
    request: str,
    plan_output: object | None,
) -> None:
    data = _load_state()
    contexts = data.setdefault("plan_contexts", {})
    entry = {
        "ts": int(time.time()),
        "engine": engine,
        "model": get_model_for_engine(engine),
        "cwd": cwd or "",
        "branch": _safe_current_branch(cwd),
        "request": request.strip(),
        "plan": _tail_text(_coerce_text(plan_output), max_chars=PLAN_CONTEXT_MAX_CHARS),
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
    if previous and (not previous.get("engine") or previous.get("engine") == engine):
        prev_cwd = (previous.get("cwd") or "").strip()
        if not cwd or not prev_cwd or prev_cwd == cwd:
            prev_request = (previous.get("request") or "").strip()
            prev_plan = (previous.get("plan") or "").strip()
            lines.append("Existing saved plan context:")
            if prev_request:
                lines.append(f"Previous request: {prev_request}")
            if prev_plan:
                lines.append("Previous plan:")
                lines.append(prev_plan)
            lines.append("Update and replace that plan using the new request below.")
    lines.append("Planning request:")
    lines.append(request.strip())
    return "\n".join(line for line in lines if line)


def build_do_prompt(plan_ctx: dict, request: str) -> str:
    saved_request = (plan_ctx.get("request") or "").strip()
    saved_plan = (plan_ctx.get("plan") or "").strip()
    do_request = request.strip() or "Execute the saved plan now."
    lines = [
        "Execute the saved plan below in this repository.",
        "Do the implementation now; do not respond with only another plan.",
        "Run relevant checks for your changes and include a concise summary.",
    ]
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

    saved_diff = (ctx.get("diff_stat") or "").strip()
    current_diff = ""
    status_lines: list[str] = []
    status_known = False
    if cwd and pathlib.Path(cwd).exists():
        current_diff = get_diff_stat(cwd)
        status_lines = get_status_porcelain(cwd)
        status_known = True

    lines = [
        "You are resuming after a timeout. The engine may have lost context.",
        "Use the saved context below to continue accurately.",
        f"Original task: {ctx.get('task', '').strip()}",
        f"Repo: {ctx.get('cwd', '').strip()}",
        f"Branch: {ctx.get('branch', '').strip()}",
        f"Saved diff at timeout: {saved_diff}" if saved_diff else "",
        f"Current diff now: {current_diff}" if current_diff else "",
    ]
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
    lines.append("Important: timeout output can claim changes that did not persist. "
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


def parse_engine_and_task(content: str) -> tuple[str, str]:
    lower = content.lower()
    for prefix in ("claude:", "cc:", "claude code:"):
        if lower.startswith(prefix):
            return "claude", content[len(prefix):].strip()
    for prefix in ("codex:", "cx:", "openai:"):
        if lower.startswith(prefix):
            return "codex", content[len(prefix):].strip()
    return DEFAULT_ENGINE, content


def get_default_engine() -> str:
    return "codex" if (DEFAULT_ENGINE or "").strip().lower() == "codex" else "claude"


def get_model_for_engine(engine: str) -> str:
    return CODEX_MODEL if engine == "codex" else CLAUDE_MODEL


def get_engine_label(engine: str) -> str:
    return "Codex CLI" if engine == "codex" else "Claude Code"


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
            ("gpt-5.3-codex", None), ("gpt-5.2-codex", None),
            ("gpt-5.1-codex-max", None), ("gpt-5.2", None), ("gpt-5.1-codex-mini", None),
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


# ── Engine runners ────────────────────────────────────────────────────────────

STATUS_REFRESH = 5  # seconds between live status updates


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
    done = asyncio.Event()
    start = time.time()

    async def read_stdout():
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            stdout_chunks.append(chunk)

    async def read_stderr():
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    async def live_update():
        last_text = ""
        while not done.is_set():
            await asyncio.sleep(STATUS_REFRESH)
            if done.is_set():
                break
            elapsed = int(time.time() - start)
            raw = b"".join(stdout_chunks).decode(errors="replace")
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
        await asyncio.wait_for(io_task, timeout=ENGINE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        partial = b"".join(stdout_chunks).decode(errors="replace")
        raise subprocess.TimeoutExpired(cmd, ENGINE_TIMEOUT, output=partial)
    finally:
        done.set()
        update_task.cancel()
        running_procs.pop(ch.id, None)
        # Final update to show completion
        elapsed = int(time.time() - start)
        try:
            await status_msg.edit(content=f"✅ {label} finished ({elapsed}s)")
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
    start = time.time()
    last_edit_time = 0.0
    last_content = ""

    async def read_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    async def stream_stdout() -> None:
        nonlocal final_result, last_edit_time, last_content
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

            now = time.time()
            if now - last_edit_time >= STATUS_REFRESH:
                elapsed = int(now - start)
                display = "".join(accumulated_text)
                tool_line = f"[tools: {', '.join(tool_activity[-5:])}]\n" if tool_activity else ""
                tail = strip_ansi(tool_line + display).strip()
                if len(tail) > 1400:
                    tail = tail[-1400:]
                new_content = f"⏳ {label} working... ({elapsed}s)\n```\n{tail or '(starting...)'}\n```"
                if new_content != last_content:
                    try:
                        await status_msg.edit(content=new_content)
                        last_content = new_content
                        last_edit_time = now
                    except discord.HTTPException:
                        pass

    try:
        await asyncio.wait_for(
            asyncio.gather(stream_stdout(), read_stderr(), proc.wait()),
            timeout=ENGINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        partial = final_result or "".join(accumulated_text)
        raise subprocess.TimeoutExpired(cmd, ENGINE_TIMEOUT, output=partial)
    finally:
        running_procs.pop(ch.id, None)
        elapsed = int(time.time() - start)
        try:
            await status_msg.edit(content=f"✅ {label} finished ({elapsed}s)")
        except discord.HTTPException:
            pass

    stderr = b"".join(stderr_chunks).decode(errors="replace")
    output = final_result or "".join(accumulated_text) or "(no output)"
    if proc.returncode != 0 and stderr:
        tail_lines = stderr.strip().split("\n")[-5:]
        output += "\n\n⚠️ stderr (tail):\n" + "\n".join(tail_lines)
    return output


async def run_claude_code(task: str, ch: discord.TextChannel, resume: bool = False, images: list[str] | None = None, cwd: str | None = None) -> str:
    """Run Claude Code. If resume=True, uses --continue to continue last session."""
    if images:
        img_lines = "\n".join(f"- {p}" for p in images)
        task = f"Examine the image(s) at the following path(s) using the Read tool:\n{img_lines}\n\n{task}"
    cmd = ["claude"]
    if resume:
        cmd.extend(["--continue", "-p", task])
    else:
        cmd.extend(["-p", task])

    cmd.extend([
        "--model", CLAUDE_MODEL,
        "--verbose",
        "--output-format", "stream-json",
        "--max-turns", "10",
    ])
    if CLAUDE_ALLOWED_TOOLS:
        cmd.append("--allowedTools")
        cmd.extend(CLAUDE_ALLOWED_TOOLS)
    if CLAUDE_DENIED_TOOLS:
        cmd.append("--disallowedTools")
        cmd.extend(CLAUDE_DENIED_TOOLS)

    return await _run_claude_streaming(cmd, ch, "Claude Code", cwd=cwd)


async def run_codex(task: str, ch: discord.TextChannel, resume: bool = False, images: list[str] | None = None, cwd: str | None = None) -> str:
    """Run Codex CLI. If resume=True, uses exec resume --last for context."""
    if resume:
        cmd = [
            "codex", "exec", "resume", "--last",
            "--full-auto",
            "--model", CODEX_MODEL,
            task,
        ]
    else:
        cmd = [
            "codex", "exec",
            "--full-auto",
            "--model", CODEX_MODEL,
            task,
        ]
    if images:
        cmd.extend(["--image", ",".join(images)])

    return await _run_with_live_output(cmd, ch, "Codex CLI", cwd=cwd)


MAX_AUTO_CONTINUES = 3  # max times to auto-resume after timeout


async def run_engine(
    engine: str,
    task: str,
    ch: discord.TextChannel,
    resume: bool = False,
    images: list[str] | None = None,
    cwd: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> str:
    runner = run_codex if engine == "codex" else run_claude_code

    if stop_event and stop_event.is_set():
        return "(stopped)"

    raw_task = task
    if resume:
        task = build_resume_prompt(task, ch.id, cwd, engine)

    try:
        output = await runner(task, ch, resume, images, cwd=cwd)
        if stop_event and stop_event.is_set():
            return "(stopped)"
        clear_resume_context(ch.id)
        return output
    except subprocess.TimeoutExpired as e:
        if stop_event and stop_event.is_set():
            return "(stopped)"
        save_resume_context(ch.id, cwd, engine, raw_task, getattr(e, "output", None), reason="timeout")
        # Auto-continue: resume the engine up to MAX_AUTO_CONTINUES times
        for attempt in range(1, MAX_AUTO_CONTINUES + 1):
            if stop_event and stop_event.is_set():
                return "(stopped)"
            auto_commit(raw_task, 0, cwd)  # save any partial work
            await ch.send(f"⏳ Timed out — saved context and auto-continuing ({attempt}/{MAX_AUTO_CONTINUES})...")
            try:
                resume_task = build_resume_prompt("continue where you left off", ch.id, cwd, engine)
                output = await runner(resume_task, ch, resume=True, cwd=cwd)
                if stop_event and stop_event.is_set():
                    return "(stopped)"
                clear_resume_context(ch.id)
                return output
            except subprocess.TimeoutExpired as e2:
                save_resume_context(ch.id, cwd, engine, raw_task, getattr(e2, "output", None), reason="timeout")
                continue
        auto_commit(raw_task, 0, cwd)
        await ch.send(f"⏰ Still not finished after {MAX_AUTO_CONTINUES} retries. "
                       f"Send a follow-up to continue manually, or `done` to review what's there.")
        return "(timed out — partial work auto-committed)"


# ── Git workflow ──────────────────────────────────────────────────────────────

def create_branch(task: str, engine: str, path: str | None = None) -> str:
    branch = f"{BRANCH_PREFIX}/{engine}/{slugify(task)}-{int(time.time()) % 100000}"
    # Branch from dev/main if available; otherwise use the default branch.
    base = _resolve_checkout_branch(path)
    if not base:
        raise RuntimeError("No base branch found (missing dev/main and no local branches).")
    checkout = run_git(["git", "checkout", base], path)
    if checkout.returncode != 0:
        err = (checkout.stderr or checkout.stdout or "").strip() or "checkout failed"
        raise RuntimeError(f"Base checkout failed for `{base}`: {err}")
    run_git(["git", "pull", "--ff-only"], path)
    run_git(["git", "checkout", "-b", branch], path)
    return branch


def auto_commit(description: str, turn: int, path: str | None = None) -> None:
    """Commit any pending changes as a WIP save after each engine turn."""
    run_git(["git", "add", "."], path)
    status = run_git(["git", "status", "--porcelain"], path).stdout.strip()
    if status:
        run_git(["git", "commit", "-m", f"WIP (turn {turn}): {description}"], path)


async def commit_and_push(branch: str, description: str, path: str | None = None) -> str:
    # Commit any remaining uncommitted changes
    run_git(["git", "add", "."], path)
    status = run_git(["git", "status", "--porcelain"], path).stdout.strip()
    if status:
        run_git(["git", "commit", "-m", f"auto: {description}"], path)
    push = run_git(["git", "push", "-u", "origin", branch], path)
    if push.returncode == 0:
        return f"✅ Pushed to `{branch}`"
    return f"❌ Push failed:\n```\n{push.stderr[-500:]}\n```"


async def discard_changes(branch: str, path: str | None = None) -> str | None:
    run_git(["git", "checkout", "."], path)
    run_git(["git", "clean", "-fd"], path)
    target = _resolve_checkout_branch(path, avoid=branch)
    if target:
        run_git(["git", "checkout", target], path)
    current = current_branch(path)
    if is_protected_branch(branch):
        return current or target
    if current and current != branch:
        run_git(["git", "branch", "-D", branch], path)
    return current or target


# ── Merge / PR ────────────────────────────────────────────────────────────────

async def merge_branch(source: str, target: str, path: str | None = None) -> str:
    run_git(["git", "fetch", "--all"], path)
    run_git(["git", "checkout", target], path)
    run_git(["git", "pull", "--ff-only"], path)

    merge = run_git(["git", "merge", source, "--no-ff",
                      "-m", f"merge {source} into {target}"], path)
    if merge.returncode != 0:
        msg = merge.stdout or merge.stderr or "unknown error"
        run_git(["git", "merge", "--abort"], path)
        return (f"❌ Merge conflict `{source}` → `{target}`:\n"
                f"```\n{truncate(msg, 500)}\n```\nAborted. Resolve at desktop.")

    push = run_git(["git", "push", "origin", target], path)
    if push.returncode != 0:
        return f"❌ Merged locally but push failed:\n```\n{push.stderr[-500:]}\n```"

    result = f"✅ Merged `{source}` → `{target}` and pushed."

    # Clean up: delete the feature branch locally and remotely after merging
    if source.startswith(f"{BRANCH_PREFIX}/"):
        if is_protected_branch(source):
            result += f"\n🛡️ Protected branch; skipping delete for `{source}`."
        else:
            run_git(["git", "branch", "-D", source], path)
            run_git(["git", "push", "origin", "--delete", source], path)
            result += f"\n🗑️ Deleted branch `{source}`."

    # Pull the target branch so the local repo stays up to date
    run_git(["git", "pull", "--ff-only"], path)

    return result


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
`<task>` — default engine ({default}) · `claude: <task>` / `cc:` · `codex: <task>` / `cx:` / `openai:`
`plan: <task>` — planning mode with default engine/model (saves plan context)
`do: [extra instructions]` — execute saved plan context, then clear it
`plan show` — show saved plan context · `plan clear` — clear saved plan context

**During a session:**
Type follow-ups freely — engine keeps context
`stop` — cancel the current run
`switch <branch|N>` — save & switch branch (creates if new)
`cwd <n>` — save & switch repo mid-session
`diff` — peek at changes · `undo` — revert last run
`context clear` — forget saved timeout context

**Ending a session:**
`done` — full diff + push prompt
`yes` / `push` — commit, push & merge · `no` / `discard` — discard
`abort` — discard immediately · `skip` — skip merge step

**After pushing:**
`merge <target>` — merge current/session/last-pushed into target
`merge src>tgt` / `merge src into tgt` — explicit source & target
`pr <target>` — open a pull request"""


def help_text_1() -> str:
    return HELP_TEXT_1_TEMPLATE.format(default=get_default_engine())

HELP_TEXT_2 = """**Branches:**
`branches` — list branches (use `N` in commands)
`branch delete <name|N> [local|remote|both] [force]`
`branch protect [list|add|remove|clear|reset]`
`switch <branch|N>` — switch branch (auto-commit if in session)

**Recovery:**
`recover` — list orphaned branches · `recover <id>` — resume
`recover drop <id>` — delete orphaned branch

**Multi-repo:**
`repos` · `cwd` / `cwd <n>` — show or switch active repo
`repo <n> status|diff|commit [msg]|push|branches`

**Config:**
`claude models` · `codex models` — list available models (numbered)
`claude model <n|name>` — e.g. `1` / `opus` / `sonnet`
`codex model <n|name>` — e.g. `1` / `gpt-5.3-codex`
`engine claude|codex` · `engine claude model <n|name>` · `engine codex model <n|name>`
`model <n|name>` — set model for default engine · `engine` — show current config

**Info:** `status` · `branches` · `pull [branch]` · `doctor` · `help`

**Login:** `claude login` · `codex login` · `openai login` · `login both`
**System:** `restart`"""

HELP_PIN_TITLE_1 = "Help (1/2)"
HELP_PIN_TITLE_2 = "Help (2/2)"


def _help_embed(title: str, text: str) -> discord.Embed:
    return discord.Embed(title=title, description=text)


async def ensure_pinned_help(channel: discord.abc.Messageable) -> bool:
    """Ensure help messages are pinned and up to date. Returns True if changed."""
    changed = False
    current_help_1 = help_text_1()

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
    print(f"🤖 Bot online as {client.user}")
    print(f"   Allowed user  : {ALLOWED_USER_ID}")
    print(f"   Default engine: {DEFAULT_ENGINE}")
    print(f"   Claude: {CLAUDE_MODEL} · Codex: {CODEX_MODEL}")
    print(f"   gh CLI        : {'yes' if has_gh_cli() else 'no'}")
    print(f"   GitHub SSH    : {'yes' if ssh_ok else '⚠️  FAILED'}")
    print(f"   Claude CLI    : {claude_status}")
    print(f"   Codex CLI     : {codex_status}")
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


@client.event
async def on_resumed():
    print(f"🤖 Bot reconnected (session resumed) as {client.user}")
    await _send_restart_confirmation()


@client.event
async def on_message(message: discord.Message):
    global DEFAULT_ENGINE, CLAUDE_MODEL, CODEX_MODEL, _restart_on_close
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

    # ── Session: done → show full diff and prompt ─────────────────────────
    if lower == "done" and session:
        diff = get_diff(cwd)
        await ch.send(f"**Full diff on `{session['branch']}`:**\n"
                       f"```diff\n{truncate(diff, 1800)}\n```")
        if "(no changes detected)" in diff:
            base = _base_branch(cwd)
            ahead = get_ahead_count(cwd)
            if ahead <= 0:
                await ch.send("No changes to commit.")
                return
            await ch.send(
                f"ℹ️ Working tree clean but branch is {ahead} commit(s) ahead of `{base}`. "
                "Continuing to review."
            )
        session["phase"] = "review"
        dev_exists = run_git(["git", "rev-parse", "--verify", DEV_BRANCH], cwd).returncode == 0
        merge_hint = f"merge to `{DEV_BRANCH}`" if dev_exists else "select a merge target"
        await ch.send(
            f"Reply **yes** to commit, push & {merge_hint}, "
            f"**skip** to push without merging, or **no** to discard."
        )
        return

    # ── Session: push approval ────────────────────────────────────────────
    if lower in ("yes", "approve", "push", "lgtm", "ship it"):
        if session and session.get("phase") == "review":
            await ch.send("⏳ Committing and pushing...")
            result = await commit_and_push(session["branch"], session["description"], cwd)
            await ch.send(result)
            if "✅" in result:
                last_pushed[ch.id] = session["branch"]
                dev_exists = run_git(["git", "rev-parse", "--verify", DEV_BRANCH], cwd).returncode == 0
                if dev_exists:
                    await ch.send(f"⏳ Merging into `{DEV_BRANCH}`...")
                    merge_result = await merge_branch(session["branch"], DEV_BRANCH, cwd)
                    await ch.send(merge_result)
                    record_state(ch.id, cwd, DEV_BRANCH)
                    await ch.send("`pr main` to create a PR to main")
                    del active_sessions[ch.id]
                else:
                    branches = [b for b in run_git(
                        ["git", "branch", "--sort=-committerdate", "--format=%(refname:short)"], cwd
                    ).stdout.strip().split("\n") if b and b != session["branch"]]
                    listing = "\n".join(f"• `{b}`" for b in branches[:10])
                    session["phase"] = "merge_target"
                    await ch.send(
                        f"⚠️ No `{DEV_BRANCH}` branch found. Which branch should `{session['branch']}` merge into?\n{listing}\nReply with the branch name, or `skip` to skip merging."
                    )
            else:
                fallback = _resolve_checkout_branch(cwd, avoid=session["branch"])
                if fallback:
                    run_git(["git", "checkout", fallback], cwd)
                    record_state(ch.id, cwd, fallback)
                del active_sessions[ch.id]
            return
        # If no session but maybe old-style pending
        await ch.send("No session awaiting approval. Send `done` first to review changes.")
        return

    if lower == "skip" and session and session.get("phase") == "review":
        await ch.send("⏳ Committing and pushing (skip merge)...")
        result = await commit_and_push(session["branch"], session["description"], cwd)
        await ch.send(result)
        if "✅" in result:
            last_pushed[ch.id] = session["branch"]
            record_state(ch.id, cwd, session["branch"])
            del active_sessions[ch.id]
            await ch.send("⏭️ Skipped merge. Use `merge <target>` or `pr <target>` any time.")
        else:
            fallback = _resolve_checkout_branch(cwd, avoid=session["branch"])
            if fallback:
                run_git(["git", "checkout", fallback], cwd)
                record_state(ch.id, cwd, fallback)
            del active_sessions[ch.id]
        return

    # ── Session: merge target selection ───────────────────────────────────
    if session and session.get("phase") == "merge_target":
        if lower == "skip":
            await ch.send("⏭️ Skipped merge. Use `merge <branch>` or `pr <branch>` any time.")
            del active_sessions[ch.id]
            return
        target_input = content.strip()
        target = resolve_branch_case_insensitive(target_input, cwd) or target_input
        check = run_git(["git", "rev-parse", "--verify", target], cwd)
        if check.returncode != 0:
            # If there are multiple case-insensitive matches, ask for exact name.
            branches = get_branch_list(cwd)
            ci_matches = [b for b in branches if b.lower() == target_input.lower()]
            if len(ci_matches) > 1:
                listing = "\n".join(f"• `{b}`" for b in ci_matches[:10])
                await ch.send(
                    f"Multiple branches match `{target_input}` (case-insensitive). "
                    f"Reply with the exact branch name:\n{listing}"
                )
                return
            branches = [b for b in run_git(
                ["git", "branch", "--sort=-committerdate", "--format=%(refname:short)"], cwd
            ).stdout.strip().split("\n") if b and b != session["branch"]]
            listing = "\n".join(f"• `{b}`" for b in branches[:10])
            await ch.send(f"Branch `{target}` not found. Pick one:\n{listing}\nOr `skip` to skip.")
            return
        await ch.send(f"⏳ Merging into `{target}`...")
        merge_result = await merge_branch(session["branch"], target, cwd)
        await ch.send(merge_result)
        record_state(ch.id, cwd, target)
        del active_sessions[ch.id]
        return

    # ── Session: discard ──────────────────────────────────────────────────
    if lower in ("no", "reject", "discard", "nah"):
        if session and session.get("phase") == "review":
            base = await discard_changes(session["branch"], cwd)
            del active_sessions[ch.id]
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
            del active_sessions[ch.id]
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
        run_git(["git", "checkout", "."], cwd)
        run_git(["git", "clean", "-fd"], cwd)
        session["turns"] = max(0, session["turns"] - 1)
        await ch.send("↩️ Reverted last changes. Send another instruction or `diff` to check.")
        return

    # ── Merge commands ────────────────────────────────────────────────────
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
        await ch.send(await merge_branch(src, tgt, cwd))
        record_state(ch.id, cwd, tgt)
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
    # Accepts: "claude login", "cc login", "codex login", "cx login", "login both"
    _is_claude_login = lower in ("claude login", "cc login")
    _is_codex_login  = lower in ("codex login", "cx login", "openai login")
    _is_both_login   = lower == "login both"
    if _is_claude_login or _is_codex_login or _is_both_login:
        if _login_lock.get(ch.id):
            await ch.send("⏳ A login is already in progress in this channel.")
            return
        _login_lock[ch.id] = True
        try:
            if _is_claude_login or _is_both_login:
                await login_claude(ch)
            if _is_codex_login or _is_both_login:
                await login_codex(ch)
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
            sess_info = (f"\n📝 Active session: **{session['engine']}** · "
                         f"{session['turns']} turn(s)")
        await ch.send(f"📍 `{cwd}`\n🌿 `{br}`{sess_info}\n"
                       f"```\n{st or '(clean)'}\n```")
        return

    if lower == "doctor":
        ssh_ok = check_github_ssh()
        claude_ok, claude_status = check_claude_cli()
        codex_ok, codex_status = check_codex_cli()
        codex_trusted = _load_codex_trusted_dirs()

        await ch.send(
            "🩺 **Diagnostics**\n"
            f"GitHub SSH: {'✅ OK' if ssh_ok else '⚠️ FAILED'}\n"
            f"Claude CLI: {'✅' if claude_ok else '⚠️'} {claude_status}\n"
            f"Codex CLI: {'✅' if codex_ok else '⚠️'} {codex_status}"
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

    if lower in ("context clear", "resume clear", "clear context"):
        cleared = clear_resume_context(ch.id)
        if cleared:
            await ch.send("🧹 Cleared saved resume context for this channel.")
        else:
            await ch.send("No saved resume context to clear.")
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

    if lower.startswith("plan:"):
        plan_request = content.split(":", 1)[1].strip()
        if not plan_request:
            await ch.send("Usage: `plan: <task>`")
            return

        engine = get_default_engine()
        label = get_engine_label(engine)
        model = get_model_for_engine(engine)
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
            )
        except Exception as e:
            await ch.send(f"❌ {label} planning error: `{e}`")
            return
        finally:
            stop_events.pop(ch.id, None)

        if stop_event.is_set():
            await ch.send("🛑 Planning stopped.")
            return

        save_plan_context(ch.id, cwd, engine, plan_request, output)
        await ch.send(f"**{label} Plan:**\n```\n{truncate(output, 1800)}\n```")
        await ch.send("💾 Saved plan context. Run `do:` to execute it.")
        return

    if lower.startswith("do:"):
        plan_ctx = load_plan_context(ch.id)
        if not plan_ctx:
            await ch.send("No saved plan context for this channel. Run `plan: <task>` first.")
            return

        do_request = content.split(":", 1)[1].strip()
        engine = get_default_engine()
        label = get_engine_label(engine)
        model = get_model_for_engine(engine)
        plan_cwd = (plan_ctx.get("cwd") or "").strip() or cwd
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

            if session:
                await discard_changes(session["branch"], cwd)
                del active_sessions[ch.id]
                await ch.send("⚠️ Previous session discarded before executing saved plan.")
                session = None

            cwd = plan_cwd
            channel_cwd[ch.id] = cwd
            await ch.send(
                f"🚀 Executing saved plan with **{label}** (`{model}`) on `{cwd}`...\n"
                f"> {truncate(exec_description, 200)}"
            )

            try:
                branch = create_branch(exec_description, engine, cwd)
            except Exception as e:
                await ch.send(f"❌ Branch creation failed: `{e}`")
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
                )
            except Exception as e:
                await ch.send(f"❌ {label} error: `{e}`")
                await discard_changes(branch, cwd)
                return
            finally:
                stop_events.pop(ch.id, None)

            if stop_event.is_set():
                await discard_changes(branch, cwd)
                return

            clear_saved_plan = True

            # If no files changed, clean up the branch and skip starting a session.
            if not run_git(["git", "status", "--porcelain"], cwd).stdout.strip():
                base = _resolve_checkout_branch(cwd, avoid=branch)
                if base:
                    run_git(["git", "checkout", base], cwd)
                    run_git(["git", "branch", "-D", branch], cwd)
                    record_state(ch.id, cwd, base)
                else:
                    record_state(ch.id, cwd, current_branch(cwd) or branch)
                await ch.send(f"**{label}:**\n```\n{truncate(output, 1800)}\n```")
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
            }

            auto_commit(exec_description, 1, cwd)
            await ch.send(f"**{label}:**\n```\n{truncate(output, 1800)}\n```")
            stat = get_diff_stat(cwd)
            await ch.send(f"📊 {stat}\n"
                          f"Send a follow-up to keep iterating, `diff` to inspect, "
                          f"or `done` when finished.")
            return
        finally:
            if clear_saved_plan and clear_plan_context(ch.id):
                await ch.send("🧹 Cleared saved plan context.")

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
        arg = lower[4:].strip()
        if arg:
            branch = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(arg, arg)
        else:
            branch = _resolve_checkout_branch(cwd) or current_branch(cwd) or DEV_BRANCH
        if not branch:
            await ch.send("❌ No base branch found to pull.")
            return
        await ch.send(f"⏳ Pulling `{branch}` from remote...")
        fetch = run_git(["git", "fetch", "origin", branch], cwd)
        if fetch.returncode != 0:
            await ch.send(f"❌ Fetch failed:\n```\n{fetch.stderr.strip()}\n```")
            return
        pull = run_git(["git", "pull", "origin", branch], cwd)
        if pull.returncode != 0:
            await ch.send(f"❌ Pull failed:\n```\n{pull.stderr.strip()}\n```")
            return
        await ch.send(f"✅ `{branch}` is up to date.\n```\n{pull.stdout.strip() or pull.stderr.strip()}\n```")
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
            await ch.send("Usage: `repo <n> status|diff|commit [msg]|push`")
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
            await ch.send("Usage: `repo <n> status|diff|commit [msg]|push|branches`")
        return

    # ── Switch active working directory ───────────────────────────────────
    if lower.startswith("cwd"):
        arg = lower[3:].strip()
        if not arg:
            proj_label = next((l for l, p in GIT_PROJECTS if p == cwd), cwd)
            await ch.send(f"Active repo: **{proj_label}** (`{cwd}`)\nUse `cwd <n>` to switch.")
            return
        proj = resolve_project(arg)
        if proj is None:
            await ch.send(f"Project `{arg}` not found. Use `repos` to list them.")
            return
        label, path = proj
        if session:
            # Auto-commit current work before switching repos
            auto_commit(session["description"], session["turns"], cwd)
            new_branch = current_branch(path)
            session["cwd"] = path
            session["branch"] = new_branch
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
            auto_commit(session["description"], session["turns"], cwd)
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
            await ch.send(f"📊 {stat or 'clean'}\nContinue with a follow-up or `done` when finished.")
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
        r"^engine\s+(claude|cc|codex|cx|openai)\s+model(?:\s+(.+))?$",
        content,
        flags=re.IGNORECASE,
    )
    if engine_model_match:
        engine_token = engine_model_match.group(1).lower()
        selector = (engine_model_match.group(2) or "").strip()
        target_engine = "claude" if engine_token in ("claude", "cc") else "codex"
        if not selector:
            await ch.send(
                "Usage: `engine claude model <n|name>` or `engine codex model <n|name>`"
            )
            return
        if target_engine == "claude":
            models = await get_claude_models()
            selected_model, err = resolve_model_selector(selector, models)
            if err:
                await ch.send(f"❌ {err}")
                return
            DEFAULT_ENGINE = "claude"
            CLAUDE_MODEL = selected_model or CLAUDE_MODEL
            _save_runtime_config()
            await ch.send(
                f"✅ Default engine set to **claude** — model `{CLAUDE_MODEL}`"
            )
            return
        models = get_codex_models()
        selected_model, err = resolve_model_selector(selector, models)
        if err:
            await ch.send(f"❌ {err}")
            return
        DEFAULT_ENGINE = "codex"
        CODEX_MODEL = selected_model or CODEX_MODEL
        _save_runtime_config()
        await ch.send(
            f"✅ Default engine set to **codex** — model `{CODEX_MODEL}`"
        )
        return
    engine_only_match = re.match(
        r"^engine\s+(claude|cc|codex|cx|openai)$",
        content,
        flags=re.IGNORECASE,
    )
    if engine_only_match:
        engine_token = engine_only_match.group(1).lower()
        target_engine = "claude" if engine_token in ("claude", "cc") else "codex"
        DEFAULT_ENGINE = target_engine
        _save_runtime_config()
        model = CLAUDE_MODEL if target_engine == "claude" else CODEX_MODEL
        await ch.send(
            f"✅ Default engine set to **{target_engine}** — current model `{model}`"
        )
        return
    if lower.startswith("engine "):
        await ch.send(
            "Usage: `engine`, `engine claude`, `engine codex`, "
            "`engine claude model <n|name>`, or `engine codex model <n|name>`"
        )
        return

    if lower == "engine":
        claude_models = await get_claude_models()
        codex_models = get_codex_models()
        claude_list = " · ".join(f"`{mid}`" for mid, _ in claude_models)
        codex_list = " · ".join(f"`{slug}`" for slug, _ in codex_models)
        await ch.send(
            f"Default: **{DEFAULT_ENGINE}**\n"
            f"Claude: `{CLAUDE_MODEL}` · Codex: `{CODEX_MODEL}`\n\n"
            f"**Available models:**\n"
            f"Claude: {claude_list}\n"
            f"Codex: {codex_list}"
        )
        return

    # ── Model listing ────────────────────────────────────────────────────
    # Accepts: "claude models", "cc models", "codex models", "cx models"
    if lower in ("claude models", "cc models"):
        models = await get_claude_models()
        listing = "\n".join(
            f"{idx}. {'▶ ' if mid == CLAUDE_MODEL else ''}`{mid}`"
            + (f" ({display_name})" if display_name != mid else "")
            for idx, (mid, display_name) in enumerate(models, start=1)
        )
        await ch.send(
            f"**Claude models** (current: `{CLAUDE_MODEL}`):\n{listing}\n\n"
            "Switch with `claude model <n|name>` or `engine claude model <n|name>`"
        )
        return
    if lower in ("codex models", "cx models"):
        models = get_codex_models()
        def _ctx_label(ctx: int | None) -> str:
            if ctx is None:
                return ""
            return f" ({ctx // 1000}K ctx)"
        listing = "\n".join(
            f"{idx}. {'▶ ' if slug == CODEX_MODEL else ''}`{slug}`{_ctx_label(ctx)}"
            for idx, (slug, ctx) in enumerate(models, start=1)
        )
        await ch.send(
            f"**Codex models** (current: `{CODEX_MODEL}`):\n{listing}\n\n"
            "Switch with `codex model <n|name>` or `engine codex model <n|name>`"
        )
        return

    # ── Model change ─────────────────────────────────────────────────────
    # Accepts: "claude model <n|name>", "cc model <n|name>", "codex model <n|name>", "cx model <n|name>"
    _model_prefixes = {
        "claude model ": "claude", "cc model ": "claude",
        "codex model ": "codex",   "cx model ": "codex",
    }
    for _pfx, _engine in _model_prefixes.items():
        if lower.startswith(_pfx):
            selector = content[len(_pfx):].strip()
            if not selector:
                await ch.send(f"Usage: `{_pfx.strip()} <n|name>`\nCurrent — Claude: `{CLAUDE_MODEL}` · Codex: `{CODEX_MODEL}`")
                return
            if _engine == "claude":
                models = await get_claude_models()
                selected_model, err = resolve_model_selector(selector, models)
                if err:
                    await ch.send(f"❌ {err}")
                    return
                CLAUDE_MODEL = selected_model or CLAUDE_MODEL
                _save_runtime_config()
                await ch.send(f"✅ Claude model set to `{CLAUDE_MODEL}`")
            else:
                models = get_codex_models()
                selected_model, err = resolve_model_selector(selector, models)
                if err:
                    await ch.send(f"❌ {err}")
                    return
                CODEX_MODEL = selected_model or CODEX_MODEL
                _save_runtime_config()
                await ch.send(f"✅ Codex model set to `{CODEX_MODEL}`")
            return

    # ── Default model change ────────────────────────────────────────────
    # Accepts: "model <n|name>", "default model <n|name>"
    if lower == "model" or lower.startswith("model ") or lower == "default model" or lower.startswith("default model "):
        if lower.startswith("default model"):
            prefix = "default model"
        else:
            prefix = "model"
        new_model = content[len(prefix):].strip()
        default_engine = (DEFAULT_ENGINE or "").strip().lower()
        if not new_model:
            if default_engine == "claude":
                current = CLAUDE_MODEL
            elif default_engine == "codex":
                current = CODEX_MODEL
            else:
                current = None
            if current:
                await ch.send(
                    f"Usage: `{prefix} <n|name>`\n"
                    f"Default engine: `{DEFAULT_ENGINE}` · Current model: `{current}`\n"
                    f"Use `claude model <n|name>` / `codex model <n|name>` to set explicitly."
                )
            else:
                await ch.send(
                    f"Usage: `{prefix} <n|name>`\n"
                    f"DEFAULT_ENGINE is `{DEFAULT_ENGINE}`. Use `claude model <n|name>` or `codex model <n|name>`."
                )
            return
        if default_engine == "claude":
            models = await get_claude_models()
            selected_model, err = resolve_model_selector(new_model, models)
            if err:
                await ch.send(f"❌ {err}")
                return
            CLAUDE_MODEL = selected_model or CLAUDE_MODEL
            _save_runtime_config()
            await ch.send(f"✅ Default engine is **claude** — model set to `{CLAUDE_MODEL}`")
            return
        if default_engine == "codex":
            models = get_codex_models()
            selected_model, err = resolve_model_selector(new_model, models)
            if err:
                await ch.send(f"❌ {err}")
                return
            CODEX_MODEL = selected_model or CODEX_MODEL
            _save_runtime_config()
            await ch.send(f"✅ Default engine is **codex** — model set to `{CODEX_MODEL}`")
            return
        await ch.send(
            f"❌ DEFAULT_ENGINE is `{DEFAULT_ENGINE}`. Use `claude model <n|name>` or `codex model <n|name>`."
        )
        return

    # ── Recover orphaned branches ────────────────────────────────────────
    if lower == "recover":
        result = run_git(["git", "branch", "--sort=-committerdate",
                          "--format=%(refname:short)"])
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

        # Resolve short ID (trailing digits) to full branch name
        if not arg.startswith(f"{BRANCH_PREFIX}/"):
            result = run_git(["git", "branch", "--sort=-committerdate",
                              "--format=%(refname:short)"])
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
            run_git(["git", "branch", "-D", arg], cwd)
            run_git(["git", "push", "origin", "--delete", arg], cwd)
            await ch.send(f"🗑️ Deleted `{arg}` locally and remotely.")
            return

        branch = arg
        # Check branch exists
        check = run_git(["git", "rev-parse", "--verify", branch], cwd)
        if check.returncode != 0:
            await ch.send(f"Branch `{branch}` not found.")
            return
        if session:
            await discard_changes(session["branch"], cwd)
            await ch.send("⚠️ Previous session discarded.\n")
        run_git(["git", "checkout", branch], cwd)
        # Parse engine from branch name (auto/engine/slug-timestamp)
        parts = branch.split("/")
        engine = parts[1] if len(parts) >= 3 else DEFAULT_ENGINE
        active_sessions[ch.id] = {
            "branch": branch,
            "engine": engine,
            "description": "recovered session",
            "turns": 0,
            "phase": "working",
            "cwd": cwd,
        }
        diff_stat = get_diff_stat(cwd)
        await ch.send(f"♻️ Recovered session on `{branch}`\n📊 {diff_stat}\n"
                       f"Send a follow-up, `diff` to inspect, or `done` when finished.")
        return

    # ── Follow-up in active session ───────────────────────────────────────
    if session and session.get("phase") != "review":
        engine = session["engine"]
        label = "Claude Code" if engine == "claude" else "Codex CLI"
        session["turns"] += 1

        images = await download_attachments(message)
        follow_up_task = content or "Describe and analyze these images"

        await ch.send(f"🔄 **{label}** follow-up (turn {session['turns']})...\n"
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
            )
        except Exception as e:
            await ch.send(f"❌ Error: `{e}`")
            return
        finally:
            stop_events.pop(ch.id, None)

        if stop_event.is_set():
            return

        auto_commit(session["description"], session["turns"], cwd)
        await ch.send(f"**{label}:**\n```\n{truncate(output, 1800)}\n```")
        stat = get_diff_stat(cwd)
        await ch.send(f"📊 {stat}\n"
                       f"Send another follow-up, `diff` to inspect, `undo` to revert, "
                       f"or `done` when finished.")
        return

    # ── New task → start a session ────────────────────────────────────────
    images = await download_attachments(message)
    engine, task = parse_engine_and_task(content) if content else (DEFAULT_ENGINE, "")
    if not task and images:
        task = "Describe and analyze these images"
    if not task:
        await ch.send("Give me a task to work on.")
        return

    # Clean up any leftover session
    if session:
        await discard_changes(session["branch"], cwd)
        await ch.send("⚠️ Previous session discarded.\n")

    label = "Claude Code" if engine == "claude" else "Codex CLI"
    await ch.send(f"🧠 **{label}** starting on `{cwd}`...\n> {truncate(task, 200)}"
                   + (f"\n📎 {len(images)} image(s) attached" if images else ""))

    try:
        branch = create_branch(task, engine, cwd)
    except Exception as e:
        await ch.send(f"❌ Branch creation failed: `{e}`")
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
        )
    except Exception as e:
        await ch.send(f"❌ {label} error: `{e}`")
        await discard_changes(branch, cwd)
        return
    finally:
        stop_events.pop(ch.id, None)

    if stop_event.is_set():
        await discard_changes(branch, cwd)
        return

    # If no files changed, clean up the branch and skip starting a session
    if not run_git(["git", "status", "--porcelain"], cwd).stdout.strip():
        base = _resolve_checkout_branch(cwd, avoid=branch)
        if base:
            run_git(["git", "checkout", base], cwd)
            run_git(["git", "branch", "-D", branch], cwd)
            record_state(ch.id, cwd, base)
        else:
            record_state(ch.id, cwd, current_branch(cwd) or branch)
        await ch.send(f"**{label}:**\n```\n{truncate(output, 1800)}\n```")
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
    }

    auto_commit(task, 1, cwd)
    await ch.send(f"**{label}:**\n```\n{truncate(output, 1800)}\n```")
    stat = get_diff_stat(cwd)
    await ch.send(f"📊 {stat}\n"
                   f"Send a follow-up to keep iterating, `diff` to inspect, "
                   f"or `done` when finished.")


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
