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
CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.2-codex")

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

# Track login processes so we don't run two at once
_login_lock: dict[int, bool] = {}  # channel_id → True while login in progress
_restart_on_close = False

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_authorised(msg: discord.Message) -> bool:
    return msg.author.id == ALLOWED_USER_ID


def slugify(text: str, max_len: int = 40) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in text.lower())
    return (slug.strip("-")[:max_len].rstrip("-")) or "task"


def run_git(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=REPO_PATH, capture_output=True, text=True, timeout=60,
    )


def run_git_in(cmd: list[str], path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=path, capture_output=True, text=True, timeout=60,
    )


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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def truncate(text: str, limit: int = MAX_DIFF_CHARS) -> str:
    if len(text) <= limit:
        return text
    h = limit // 2 - 20
    return text[:h] + "\n\n... (truncated) ...\n\n" + text[-h:]


def current_branch() -> str:
    return run_git(["git", "branch", "--show-current"]).stdout.strip()


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


def _base_branch() -> str:
    """Return the base branch to diff against (dev if it exists, else main)."""
    check = run_git(["git", "rev-parse", "--verify", DEV_BRANCH])
    return DEV_BRANCH if check.returncode == 0 else MAIN_BRANCH


def get_diff() -> str:
    base = _base_branch()
    # Committed changes on this branch vs base
    committed = run_git(["git", "diff", f"{base}...HEAD"]).stdout or ""
    # Plus any uncommitted changes not yet auto-committed
    uncommitted = run_git(["git", "diff"]).stdout or ""
    staged = run_git(["git", "diff", "--cached"]).stdout or ""
    untracked = run_git(["git", "ls-files", "--others", "--exclude-standard"]).stdout.strip()
    combined = committed + uncommitted + staged
    if untracked:
        combined += f"\n\nNew files:\n{untracked}"
    return combined.strip() or "(no changes detected)"


def get_diff_stat() -> str:
    """Short summary: 3 files changed, 12 insertions, 2 deletions."""
    base = _base_branch()
    stat = run_git(["git", "diff", "--stat", f"{base}...HEAD"]).stdout.strip()
    # Also include any uncommitted changes
    uncommitted_stat = run_git(["git", "diff", "--stat"]).stdout.strip()
    untracked = run_git(["git", "ls-files", "--others", "--exclude-standard"]).stdout.strip()
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


async def _run_with_live_output(cmd: list[str], ch: discord.TextChannel, label: str) -> str:
    """Run a subprocess, live-updating a single Discord message with output."""
    status_msg = await ch.send(f"⚙️ {label} started...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=REPO_PATH,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

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


async def run_claude_code(task: str, ch: discord.TextChannel, resume: bool = False, images: list[str] | None = None) -> str:
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

    return await _run_with_live_output(cmd, ch, "Claude Code")


async def run_codex(task: str, ch: discord.TextChannel, resume: bool = False, images: list[str] | None = None) -> str:
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

    return await _run_with_live_output(cmd, ch, "Codex CLI")


MAX_AUTO_CONTINUES = 3  # max times to auto-resume after timeout


async def run_engine(engine: str, task: str, ch: discord.TextChannel, resume: bool = False, images: list[str] | None = None) -> str:
    if engine == "codex":
        runner = run_codex
    else:
        runner = run_claude_code

    try:
        return await runner(task, ch, resume, images)
    except subprocess.TimeoutExpired:
        # Auto-continue: resume the engine up to MAX_AUTO_CONTINUES times
        for attempt in range(1, MAX_AUTO_CONTINUES + 1):
            auto_commit(task, 0)  # save any partial work
            await ch.send(f"⏳ Timed out — auto-continuing ({attempt}/{MAX_AUTO_CONTINUES})...")
            try:
                return await runner("continue where you left off", ch, resume=True)
            except subprocess.TimeoutExpired:
                continue
        auto_commit(task, 0)
        await ch.send(f"⏰ Still not finished after {MAX_AUTO_CONTINUES} retries. "
                       f"Send a follow-up to continue manually, or `done` to review what's there.")
        return "(timed out — partial work auto-committed)"


# ── Git workflow ──────────────────────────────────────────────────────────────

def create_branch(task: str, engine: str) -> str:
    branch = f"{BRANCH_PREFIX}/{engine}/{slugify(task)}-{int(time.time()) % 100000}"
    # Branch from dev if it exists, otherwise main — keeps new branches
    # up to date with previously merged work and avoids conflicts.
    base = DEV_BRANCH
    check = run_git(["git", "rev-parse", "--verify", base])
    if check.returncode != 0:
        base = MAIN_BRANCH
    run_git(["git", "checkout", base])
    run_git(["git", "pull", "--ff-only"])
    run_git(["git", "checkout", "-b", branch])
    return branch


def auto_commit(description: str, turn: int) -> None:
    """Commit any pending changes as a WIP save after each engine turn."""
    run_git(["git", "add", "."])
    status = run_git(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        run_git(["git", "commit", "-m", f"WIP (turn {turn}): {description}"])


async def commit_and_push(branch: str, description: str) -> str:
    # Commit any remaining uncommitted changes
    run_git(["git", "add", "."])
    status = run_git(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        run_git(["git", "commit", "-m", f"auto: {description}"])
    push = run_git(["git", "push", "-u", "origin", branch])
    if push.returncode == 0:
        return f"✅ Pushed to `{branch}`"
    return f"❌ Push failed:\n```\n{push.stderr[-500:]}\n```"


async def discard_changes(branch: str) -> None:
    run_git(["git", "checkout", "."])
    run_git(["git", "clean", "-fd"])
    run_git(["git", "checkout", DEV_BRANCH])
    run_git(["git", "branch", "-D", branch])


# ── Merge / PR ────────────────────────────────────────────────────────────────

async def merge_branch(source: str, target: str) -> str:
    run_git(["git", "fetch", "--all"])
    run_git(["git", "checkout", target])
    run_git(["git", "pull", "--ff-only"])

    merge = run_git(["git", "merge", source, "--no-ff",
                      "-m", f"merge {source} into {target}"])
    if merge.returncode != 0:
        msg = merge.stdout or merge.stderr or "unknown error"
        run_git(["git", "merge", "--abort"])
        return (f"❌ Merge conflict `{source}` → `{target}`:\n"
                f"```\n{truncate(msg, 500)}\n```\nAborted. Resolve at desktop.")

    push = run_git(["git", "push", "origin", target])
    if push.returncode != 0:
        return f"❌ Merged locally but push failed:\n```\n{push.stderr[-500:]}\n```"

    result = f"✅ Merged `{source}` → `{target}` and pushed."

    # Clean up: delete the feature branch locally and remotely after merging
    if source.startswith(f"{BRANCH_PREFIX}/"):
        run_git(["git", "branch", "-D", source])
        run_git(["git", "push", "origin", "--delete", source])
        result += f"\n🗑️ Deleted branch `{source}`."

    # Pull the target branch so the local repo stays up to date
    run_git(["git", "pull", "--ff-only"])

    return result


async def create_pr(source: str, target: str, title: str) -> str:
    if not has_gh_cli():
        return "❌ `gh` not installed. Run `sudo apt install gh && gh auth login`."

    result = run_git([
        "gh", "pr", "create",
        "--base", target, "--head", source,
        "--title", title,
        "--body", f"Auto-generated from Discord bot.\n\nTask: {title}",
    ])
    if result.returncode == 0:
        return f"✅ PR created: {result.stdout.strip()}"
    return f"❌ PR failed:\n```\n{result.stderr[-500:]}\n```"


# ── Discord handlers ─────────────────────────────────────────────────────────

HELP_TEXT = """**Starting a session:**
`<task>` — start with default engine ({default})
`claude: <task>` / `cc: <task>` — start with Claude Code
`codex: <task>` / `cx: <task>` — start with Codex CLI

**During a session** (iterative back-and-forth):
Just type your follow-up — the engine keeps context
`diff` — see current changes so far
`undo` — revert last engine run (git checkout)

**Ending a session:**
`done` — see full diff + push prompt
`yes` / `push` — commit, push & auto-merge to dev
`no` / `discard` — discard all changes
`abort` — discard and end session immediately

**After pushing:**
`merge dev>main` — merge dev → main
`pr main` — create a PR to main

**Recovery:**
`recover` — list orphaned feature branches
`recover <id>` — resume by short ID (last digits) or full name
`recover drop <id>` — delete by short ID or full name

**Config:**
`model claude <name>` — change Claude model (e.g. opus, sonnet, haiku)
`model codex <name>` — change Codex model
`engine` — show current models

**Multi-repo:**
`repos` — list all configured git projects
`repo <n> status` — git status for project n
`repo <n> diff` — diff for project n
`repo <n> commit [msg]` — stage all & commit in project n
`repo <n> push` — push project n

**Info:**
`status` · `branches` · `help` · `restart`
`pull` — pull latest dev from remote
`pull main` — pull latest main from remote

**Login:**
`login claude` — authenticate Claude Code (browser OAuth)
`login codex` — authenticate Codex CLI (device code)
`login both` — authenticate both""".format(default=DEFAULT_ENGINE)


@tree.command(name="help", description="Show all available bot commands")
async def slash_help(interaction: discord.Interaction):
    if interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("Not authorised.", ephemeral=True)
        return
    await interaction.response.send_message(HELP_TEXT)


@client.event
async def on_ready():
    await tree.sync()
    ssh_ok = check_github_ssh()
    print(f"🤖 Bot online as {client.user}")
    print(f"   Allowed user : {ALLOWED_USER_ID}")
    print(f"   Repo         : {REPO_PATH}")
    print(f"   Default engine: {DEFAULT_ENGINE}")
    print(f"   Claude: {CLAUDE_MODEL} · Codex: {CODEX_MODEL}")
    print(f"   gh CLI       : {'yes' if has_gh_cli() else 'no'}")
    print(f"   GitHub SSH   : {'yes' if ssh_ok else '⚠️  FAILED'}")
    if not ssh_ok:
        print(f"\n⚠️  Cannot connect to GitHub via SSH.")
        print(f"   Fix:   eval \"$(ssh-agent -s)\" && ssh-add ~/.ssh/id_ed25519")
        print(f"   Test:  ssh -T git@github.com")
    print(f"   Slash commands synced")


@client.event
async def on_resumed():
    print(f"🤖 Bot reconnected (session resumed) as {client.user}")


@client.event
async def on_message(message: discord.Message):
    global CLAUDE_MODEL, CODEX_MODEL
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

    # ── Session: done → show full diff and prompt ─────────────────────────
    if lower == "done" and session:
        diff = get_diff()
        await ch.send(f"**Full diff on `{session['branch']}`:**\n"
                       f"```diff\n{truncate(diff, 1800)}\n```")
        if "(no changes detected)" in diff:
            await ch.send("No changes to commit.")
            return
        session["phase"] = "review"
        await ch.send("Reply **yes** to commit, push & merge to dev, or **no** to discard.")
        return

    # ── Session: push approval ────────────────────────────────────────────
    if lower in ("yes", "approve", "push", "lgtm", "ship it"):
        if session and session.get("phase") == "review":
            await ch.send("⏳ Committing and pushing...")
            result = await commit_and_push(session["branch"], session["description"])
            await ch.send(result)
            if "✅" in result:
                last_pushed[ch.id] = session["branch"]
                # Auto-merge into dev
                await ch.send(f"⏳ Merging into `{DEV_BRANCH}`...")
                merge_result = await merge_branch(session["branch"], DEV_BRANCH)
                await ch.send(merge_result)
                await ch.send(f"`pr main` to create a PR to main")
            else:
                run_git(["git", "checkout", DEV_BRANCH])
            del active_sessions[ch.id]
            return
        # If no session but maybe old-style pending
        await ch.send("No session awaiting approval. Send `done` first to review changes.")
        return

    # ── Session: discard ──────────────────────────────────────────────────
    if lower in ("no", "reject", "discard", "nah"):
        if session and session.get("phase") == "review":
            await discard_changes(session["branch"])
            del active_sessions[ch.id]
            await ch.send(f"🗑️ Discarded, back on `{DEV_BRANCH}`.")
            return
        await ch.send("No session awaiting approval. Use `abort` to end an active session.")
        return

    # ── Session: abort (discard immediately) ──────────────────────────────
    if lower == "abort":
        if session:
            await discard_changes(session["branch"])
            del active_sessions[ch.id]
            await ch.send(f"🗑️ Session aborted, back on `{DEV_BRANCH}`.")
        else:
            await ch.send("No active session.")
        return

    # ── Session: diff (peek at current changes) ──────────────────────────
    if lower == "diff" and session:
        diff = get_diff()
        stat = get_diff_stat()
        await ch.send(f"**Changes so far** ({stat}):\n"
                       f"```diff\n{truncate(diff, 1800)}\n```")
        return

    # ── Session: undo (revert uncommitted changes from last run) ──────────
    if lower == "undo" and session:
        run_git(["git", "checkout", "."])
        run_git(["git", "clean", "-fd"])
        session["turns"] = max(0, session["turns"] - 1)
        await ch.send("↩️ Reverted last changes. Send another instruction or `diff` to check.")
        return

    # ── Merge commands ────────────────────────────────────────────────────
    if lower.startswith("merge "):
        target_str = lower[6:].strip()
        if ">" in target_str:
            parts = target_str.split(">", 1)
            src = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(parts[0].strip(), parts[0].strip())
            tgt = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(parts[1].strip(), parts[1].strip())
        else:
            src = last_pushed.get(ch.id)
            tgt = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(target_str, target_str)
        if not src:
            await ch.send("No recently pushed branch. Push first.")
            return
        await ch.send(f"⏳ Merging `{src}` → `{tgt}`...")
        await ch.send(await merge_branch(src, tgt))
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
        await ch.send(await create_pr(src, tgt, f"auto: {src}"))
        return

    # ── Login commands ────────────────────────────────────────────────────
    if lower.startswith("login"):
        if _login_lock.get(ch.id):
            await ch.send("⏳ A login is already in progress in this channel.")
            return

        arg = lower[5:].strip()
        _login_lock[ch.id] = True
        try:
            if arg in ("codex", "cx", "openai"):
                await login_codex(ch)
            elif arg in ("claude", "cc", ""):
                await login_claude(ch)
            elif arg == "both":
                await login_claude(ch)
                await login_codex(ch)
            else:
                await ch.send("Usage: `login claude`, `login codex`, or `login both`")
        finally:
            _login_lock.pop(ch.id, None)
        return

    # ── Info commands ─────────────────────────────────────────────────────
    if lower == "restart":
        global _restart_on_close
        await ch.send("🔄 Restarting bot...")
        _restart_on_close = True
        await client.close()
        return

    if lower == "help":
        await ch.send(HELP_TEXT)
        return

    if lower == "status":
        st = run_git(["git", "status", "--short"]).stdout.strip()
        br = current_branch()
        sess_info = ""
        if session:
            sess_info = (f"\n📝 Active session: **{session['engine']}** · "
                         f"{session['turns']} turn(s)")
        await ch.send(f"📍 `{REPO_PATH}`\n🌿 `{br}`{sess_info}\n"
                       f"```\n{st or '(clean)'}\n```")
        return

    if lower == "branches":
        result = run_git(["git", "branch", "--sort=-committerdate",
                          "--format=%(refname:short)"])
        branches = [b for b in result.stdout.strip().split("\n") if b][:10]
        listing = "\n".join(f"• `{b}`" for b in branches)
        await ch.send(f"**Recent branches:**\n{listing}")
        return

    if lower.startswith("pull"):
        arg = lower[4:].strip()
        branch = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(arg, arg) if arg else DEV_BRANCH
        await ch.send(f"⏳ Pulling `{branch}` from remote...")
        fetch = run_git(["git", "fetch", "origin", branch])
        if fetch.returncode != 0:
            await ch.send(f"❌ Fetch failed:\n```\n{fetch.stderr.strip()}\n```")
            return
        pull = run_git(["git", "pull", "origin", branch])
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
            lines.append(f"**{i}. {label}** (`{branch}`){dirty}\n   `{path}`")
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

        else:
            await ch.send("Usage: `repo <n> status|diff|commit [msg]|push`")
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
    if lower.startswith("model "):
        parts = lower[6:].strip().split(None, 1)
        if len(parts) != 2:
            await ch.send(
                f"Usage: `model claude <name>` or `model codex <name>`\n"
                f"Current — Claude: `{CLAUDE_MODEL}` · Codex: `{CODEX_MODEL}`"
            )
            return
        target, new_model = parts[0], parts[1]
        if target in ("claude", "cc"):
            CLAUDE_MODEL = new_model
            await ch.send(f"✅ Claude model set to `{CLAUDE_MODEL}`")
        elif target in ("codex", "cx"):
            CODEX_MODEL = new_model
            await ch.send(f"✅ Codex model set to `{CODEX_MODEL}`")
        else:
            await ch.send("Use `model claude <name>` or `model codex <name>`")
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
            run_git(["git", "branch", "-D", arg])
            run_git(["git", "push", "origin", "--delete", arg])
            await ch.send(f"🗑️ Deleted `{arg}` locally and remotely.")
            return

        branch = arg
        # Check branch exists
        check = run_git(["git", "rev-parse", "--verify", branch])
        if check.returncode != 0:
            await ch.send(f"Branch `{branch}` not found.")
            return
        if session:
            await discard_changes(session["branch"])
            await ch.send("⚠️ Previous session discarded.\n")
        run_git(["git", "checkout", branch])
        # Parse engine from branch name (auto/engine/slug-timestamp)
        parts = branch.split("/")
        engine = parts[1] if len(parts) >= 3 else DEFAULT_ENGINE
        active_sessions[ch.id] = {
            "branch": branch,
            "engine": engine,
            "description": "recovered session",
            "turns": 0,
            "phase": "working",
        }
        diff_stat = get_diff_stat()
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
        try:
            output = await run_engine(engine, follow_up_task, ch, resume=True, images=images)
        except Exception as e:
            await ch.send(f"❌ Error: `{e}`")
            return

        auto_commit(session["description"], session["turns"])
        await ch.send(f"**{label}:**\n```\n{truncate(output, 1800)}\n```")
        stat = get_diff_stat()
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
        await discard_changes(session["branch"])
        await ch.send("⚠️ Previous session discarded.\n")

    label = "Claude Code" if engine == "claude" else "Codex CLI"
    await ch.send(f"🧠 **{label}** starting session...\n> {truncate(task, 200)}"
                   + (f"\n📎 {len(images)} image(s) attached" if images else ""))

    try:
        branch = create_branch(task, engine)
    except Exception as e:
        await ch.send(f"❌ Branch creation failed: `{e}`")
        return

    try:
        output = await run_engine(engine, task, ch, resume=False, images=images)
    except Exception as e:
        await ch.send(f"❌ {label} error: `{e}`")
        await discard_changes(branch)
        return

    # Create session
    active_sessions[ch.id] = {
        "branch": branch,
        "engine": engine,
        "description": task,
        "turns": 1,
        "phase": "working",
    }

    auto_commit(task, 1)
    await ch.send(f"**{label}:**\n```\n{truncate(output, 1800)}\n```")
    stat = get_diff_stat()
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
