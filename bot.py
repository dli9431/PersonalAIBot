#!/usr/bin/env python3
"""
Discord → Claude Code / Codex CLI → Git bridge bot.

Supports iterative sessions: send a task, review changes, send follow-ups,
and only commit when you're satisfied. Uses --resume (Claude Code) and
exec resume --last (Codex) for multi-turn context.

Designed to run inside WSL2 on Windows.

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

# Claude Code (mutable at runtime via Discord `model` command)
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


def _init_protected_branches() -> None:
    state = _load_state()
    branches = state.get("protected_branches") or _default_protected_branches()
    _set_protected_branches(branches, save=not bool(state.get("protected_branches")))


def is_protected_branch(branch: str) -> bool:
    return branch.strip().lower() in _PROTECTED_BRANCH_KEYS


_init_protected_branches()


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
        if (pathlib.Path.home() / ".claude" / "credentials.json").exists():
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


def _base_branch(path: str | None = None) -> str:
    """Return the base branch to diff against (dev if it exists, else main)."""
    check = run_git(["git", "rev-parse", "--verify", DEV_BRANCH], path)
    return DEV_BRANCH if check.returncode == 0 else MAIN_BRANCH


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


# ── Login helpers ─────────────────────────────────────────────────────────────

async def login_codex(ch: discord.TextChannel) -> None:
    """Run `codex login --device-auth`, relay URL+code to Discord, wait for completion."""
    await ch.send("🔑 Starting Codex device login...")

    proc = await asyncio.create_subprocess_exec(
        "codex", "login", "--device-auth",
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
        raise subprocess.TimeoutExpired(cmd, ENGINE_TIMEOUT)
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


async def run_claude_code(task: str, ch: discord.TextChannel, resume: bool = False, images: list[str] | None = None, cwd: str | None = None) -> str:
    """Run Claude Code. If resume=True, uses --resume to continue last session."""
    if images:
        img_lines = "\n".join(f"- {p}" for p in images)
        task = f"Examine the image(s) at the following path(s) using the Read tool:\n{img_lines}\n\n{task}"
    cmd = ["claude"]
    if resume:
        cmd.extend(["--resume", "-p", task])
    else:
        cmd.extend(["-p", task])

    cmd.extend([
        "--model", CLAUDE_MODEL,
        "--output-format", "text",
        "--max-turns", "10",
    ])
    if CLAUDE_ALLOWED_TOOLS:
        cmd.append("--allowedTools")
        cmd.extend(CLAUDE_ALLOWED_TOOLS)
    if CLAUDE_DENIED_TOOLS:
        cmd.append("--disallowedTools")
        cmd.extend(CLAUDE_DENIED_TOOLS)

    return await _run_with_live_output(cmd, ch, "Claude Code", cwd=cwd)


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

    try:
        output = await runner(task, ch, resume, images, cwd=cwd)
        if stop_event and stop_event.is_set():
            return "(stopped)"
        return output
    except subprocess.TimeoutExpired:
        if stop_event and stop_event.is_set():
            return "(stopped)"
        # Auto-continue: resume the engine up to MAX_AUTO_CONTINUES times
        for attempt in range(1, MAX_AUTO_CONTINUES + 1):
            if stop_event and stop_event.is_set():
                return "(stopped)"
            auto_commit(task, 0, cwd)  # save any partial work
            await ch.send(f"⏳ Timed out — auto-continuing ({attempt}/{MAX_AUTO_CONTINUES})...")
            try:
                output = await runner("continue where you left off", ch, resume=True, cwd=cwd)
                if stop_event and stop_event.is_set():
                    return "(stopped)"
                return output
            except subprocess.TimeoutExpired:
                continue
        auto_commit(task, 0, cwd)
        await ch.send(f"⏰ Still not finished after {MAX_AUTO_CONTINUES} retries. "
                       f"Send a follow-up to continue manually, or `done` to review what's there.")
        return "(timed out — partial work auto-committed)"


# ── Git workflow ──────────────────────────────────────────────────────────────

def create_branch(task: str, engine: str, path: str | None = None) -> str:
    branch = f"{BRANCH_PREFIX}/{engine}/{slugify(task)}-{int(time.time()) % 100000}"
    # Branch from dev if it exists, otherwise main — keeps new branches
    # up to date with previously merged work and avoids conflicts.
    base = DEV_BRANCH
    check = run_git(["git", "rev-parse", "--verify", base], path)
    if check.returncode != 0:
        base = MAIN_BRANCH
    run_git(["git", "checkout", base], path)
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


async def discard_changes(branch: str, path: str | None = None) -> None:
    run_git(["git", "checkout", "."], path)
    run_git(["git", "clean", "-fd"], path)
    run_git(["git", "checkout", DEV_BRANCH], path)
    if is_protected_branch(branch):
        return
    run_git(["git", "branch", "-D", branch], path)


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

HELP_TEXT_1 = """**Starting a session:**
`<task>` — default engine ({default}) · `claude: <task>` / `cc:` · `codex: <task>` / `cx:`

**During a session:**
Type follow-ups freely — engine keeps context
`stop` — cancel the current run
`branch switch <branch|N>` — save & switch branch (creates if new)
`cwd <n>` — save & switch repo mid-session
`diff` — peek at changes · `undo` — revert last run

**Ending a session:**
`done` — full diff + push prompt
`yes` / `push` — commit, push & merge · `no` / `discard` — discard
`abort` — discard immediately · `skip` — skip merge step

**After pushing:**
`merge <target>` — merge current/session/last-pushed into target
`merge src>tgt` / `merge src into tgt` — explicit source & target
`pr <target>` — open a pull request""".format(default=DEFAULT_ENGINE)

HELP_TEXT_2 = """**Branches:**
`branches` — list branches (use `N` in commands)
`branch delete <name|N> [local|remote] [force]`
`branch protect [list|add|remove|clear|reset]`
`branch switch <branch|N>` — switch branch (in active session)

**Recovery:**
`recover` — list orphaned branches · `recover <id>` — resume
`recover drop <id>` — delete orphaned branch

**Multi-repo:**
`repos` · `cwd` / `cwd <n>` — show or switch active repo
`repo <n> status|diff|commit [msg]|push|branches`

**Config:**
`claude model <name>` — e.g. opus, sonnet, haiku
`codex model <name>` — e.g. gpt-5.3-codex · `engine`

**Info:** `status` · `branches` · `pull [main]` · `help`

**Login:** `claude login` · `codex login` · `login both`
**System:** `restart`"""

HELP_PIN_TITLE_1 = "Help (1/2)"
HELP_PIN_TITLE_2 = "Help (2/2)"


def _help_embed(title: str, text: str) -> discord.Embed:
    return discord.Embed(title=title, description=text)


async def ensure_pinned_help(channel: discord.abc.Messageable) -> bool:
    """Ensure help messages are pinned and up to date. Returns True if changed."""
    changed = False

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
        await channel.send(HELP_TEXT_1)
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
        (pinned_1, HELP_PIN_TITLE_1, HELP_TEXT_1),
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
    print(f"   Project dirs  :")
    for label, path in GIT_PROJECTS:
        p = pathlib.Path(path)
        if not p.exists():
            print(f"     ⚠️  [{label}] directory not found: {path}")
        elif run_git(["git", "rev-parse", "--git-dir"], path).returncode != 0:
            print(f"     ⚠️  [{label}] not a git repo: {path}")
        else:
            branch = current_branch(path)
            print(f"     ✓  [{label}] on '{branch}': {path}")
    if not ssh_ok:
        print(f"\n⚠️  Cannot connect to GitHub via SSH.")
        print(f"   Fix:   eval \"$(ssh-agent -s)\" && ssh-add ~/.ssh/id_ed25519")
        print(f"   Test:  ssh -T git@github.com")
    if not claude_ok:
        print(f"\n⚠️  Claude CLI unavailable: {claude_status}")
    if not codex_ok:
        print(f"\n⚠️  Codex CLI unavailable: {codex_status}")
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
    global CLAUDE_MODEL, CODEX_MODEL, _restart_on_close
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
            await ch.send("No changes to commit.")
            return
        session["phase"] = "review"
        dev_exists = run_git(["git", "rev-parse", "--verify", DEV_BRANCH], cwd).returncode == 0
        merge_hint = f"merge to `{DEV_BRANCH}`" if dev_exists else "select a merge target"
        await ch.send(f"Reply **yes** to commit, push & {merge_hint}, or **no** to discard.")
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
                run_git(["git", "checkout", DEV_BRANCH], cwd)
                del active_sessions[ch.id]
            return
        # If no session but maybe old-style pending
        await ch.send("No session awaiting approval. Send `done` first to review changes.")
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
            await discard_changes(session["branch"], cwd)
            del active_sessions[ch.id]
            await ch.send(f"🗑️ Discarded, back on `{DEV_BRANCH}`.")
            return
        await ch.send("No session awaiting approval. Use `abort` to end an active session.")
        return

    # ── Session: abort (discard immediately) ──────────────────────────────
    if lower == "abort":
        if session:
            await discard_changes(session["branch"], cwd)
            del active_sessions[ch.id]
            await ch.send(f"🗑️ Session aborted, back on `{DEV_BRANCH}`.")
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
        branch = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(arg, arg) if arg else DEV_BRANCH
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

    # ── Switch branch mid-session ─────────────────────────────────────────
    if lower.startswith("branch switch ") or lower.startswith("switch "):
        prefix_len = len("branch switch ") if lower.startswith("branch switch ") else len("switch ")
        branch_ref = content[prefix_len:].strip()  # preserve case for branch name
        if not session:
            await ch.send("No active session. Start a task first.")
            return
        # Resolve N references
        branch_name = resolve_branch(branch_ref, ch.id, cwd) or branch_ref
        # Auto-commit current work before switching
        auto_commit(session["description"], session["turns"], cwd)
        check = run_git(["git", "rev-parse", "--verify", branch_name], cwd)
        if check.returncode != 0:
            run_git(["git", "checkout", "-b", branch_name], cwd)
            await ch.send(f"🌿 Created and switched to `{branch_name}`")
        else:
            run_git(["git", "checkout", branch_name], cwd)
            await ch.send(f"🌿 Switched to `{branch_name}`")
        session["branch"] = branch_name
        record_state(ch.id, cwd, branch_name)
        stat = get_diff_stat(cwd)
        await ch.send(f"📊 {stat or 'clean'}\nContinue with a follow-up or `done` when finished.")
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

    if lower == "engine":
        await ch.send(
            f"Default: **{DEFAULT_ENGINE}**\n"
            f"Claude: `{CLAUDE_MODEL}` · Codex: `{CODEX_MODEL}`\n\n"
            f"**Available models:**\n"
            f"Claude: `opus` · `sonnet` · `haiku`\n"
            f"Codex: `gpt-5.3-codex` · `gpt-5.2-codex` · `gpt-5.1-codex-max` · `gpt-5.2` · `gpt-5.1-codex-mini`"
        )
        return

    # ── Model change ─────────────────────────────────────────────────────
    # Accepts: "claude model <name>", "cc model <name>", "codex model <name>", "cx model <name>"
    _model_prefixes = {
        "claude model ": "claude", "cc model ": "claude",
        "codex model ": "codex",   "cx model ": "codex",
    }
    for _pfx, _engine in _model_prefixes.items():
        if lower.startswith(_pfx):
            new_model = content[len(_pfx):].strip()
            if not new_model:
                await ch.send(f"Usage: `{_pfx.strip()} <name>`\nCurrent — Claude: `{CLAUDE_MODEL}` · Codex: `{CODEX_MODEL}`")
                return
            if _engine == "claude":
                CLAUDE_MODEL = new_model
                await ch.send(f"✅ Claude model set to `{CLAUDE_MODEL}`")
            else:
                CODEX_MODEL = new_model
                await ch.send(f"✅ Codex model set to `{CODEX_MODEL}`")
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
        run_git(["git", "checkout", _base_branch(cwd)], cwd)
        run_git(["git", "branch", "-D", branch], cwd)
        record_state(ch.id, cwd, _base_branch(cwd))
        await ch.send(f"**{label}:**\n```\n{truncate(output, 1800)}\n```")
        await ch.send("ℹ️ No files changed — no session started.")
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
