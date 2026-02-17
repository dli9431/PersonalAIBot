#!/usr/bin/env python3
"""
Discord → Claude Code / Codex CLI → Git bridge bot.

Receives messages from your private Discord server, routes them to either
Claude Code or OpenAI Codex CLI in non-interactive mode, and optionally
commits + pushes the changes after your approval. Supports merging to
dev/main branches and creating GitHub PRs via the gh CLI.

Designed to run inside WSL2 on Windows.

Requirements:
    pip install discord.py python-dotenv
    Optional: gh CLI (for PR creation)

Usage:
    1. Copy .env.example → .env and fill in your values.
    2. python bot.py
"""

import asyncio
import os
import subprocess
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

# ── Engine defaults ──────────────────────────────────────────────────────────

DEFAULT_ENGINE = os.getenv("DEFAULT_ENGINE", "claude")

# Claude Code
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "sonnet")
CLAUDE_ALLOWED_TOOLS = os.getenv("CLAUDE_ALLOWED_TOOLS",
    "Read Edit Write Grep Glob LS Bash(git\\ diff) Bash(git\\ status)"
).split()
CLAUDE_DENIED_TOOLS = os.getenv("CLAUDE_DENIED_TOOLS",
    "Bash(rm\\ *) Bash(sudo\\ *) Bash(curl\\ *) Bash(wget\\ *) WebFetch"
).split()

# Codex CLI
CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.2-codex")
CODEX_SANDBOX = os.getenv("CODEX_SANDBOX", "workspace-write")
CODEX_APPROVAL = os.getenv("CODEX_APPROVAL", "on-request")

ENGINE_TIMEOUT = int(os.getenv("ENGINE_TIMEOUT", "300"))

# ── Discord client setup ─────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# channel_id → {branch, description, diff, engine}
pending_approvals: dict[int, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_authorised(message: discord.Message) -> bool:
    return message.author.id == ALLOWED_USER_ID


def slugify(text: str, max_len: int = 40) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in text.lower())
    slug = slug.strip("-")[:max_len].rstrip("-")
    return slug or "task"


def run_git(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd or REPO_PATH,
        capture_output=True, text=True, timeout=60,
    )


def truncate(text: str, limit: int = MAX_DIFF_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2 - 20
    return text[:half] + "\n\n... (truncated) ...\n\n" + text[-half:]


def current_branch() -> str:
    result = run_git(["git", "branch", "--show-current"])
    return result.stdout.strip()


def has_gh_cli() -> bool:
    try:
        r = subprocess.run(["gh", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def parse_engine_and_task(content: str) -> tuple[str, str]:
    """
    Parse engine prefix from message.
    'claude: fix the bug'  → ("claude", "fix the bug")
    'codex: fix the bug'   → ("codex",  "fix the bug")
    'fix the bug'          → (DEFAULT_ENGINE, "fix the bug")
    """
    lower = content.lower()
    for prefix in ("claude:", "cc:", "claude code:"):
        if lower.startswith(prefix):
            return "claude", content[len(prefix):].strip()
    for prefix in ("codex:", "cx:", "openai:"):
        if lower.startswith(prefix):
            return "codex", content[len(prefix):].strip()
    return DEFAULT_ENGINE, content


# ── Engine runners ────────────────────────────────────────────────────────────

async def run_claude_code(task: str) -> str:
    cmd = [
        "claude", "-p", task,
        "--model", CLAUDE_MODEL,
        "--output-format", "text",
        "--max-turns", "10",
    ]
    if CLAUDE_ALLOWED_TOOLS:
        cmd.append("--allowedTools")
        cmd.extend(CLAUDE_ALLOWED_TOOLS)
    if CLAUDE_DENIED_TOOLS:
        cmd.append("--disallowedTools")
        cmd.extend(CLAUDE_DENIED_TOOLS)

    proc = await asyncio.to_thread(
        subprocess.run, cmd,
        cwd=REPO_PATH, capture_output=True, text=True,
        timeout=ENGINE_TIMEOUT,
    )
    output = proc.stdout or "(no output)"
    if proc.returncode != 0 and proc.stderr:
        output += f"\n\n⚠️ stderr:\n{proc.stderr[-500:]}"
    return output


async def run_codex(task: str) -> str:
    cmd = [
        "codex", "exec",
        "--model", CODEX_MODEL,
        "--sandbox", CODEX_SANDBOX,
        "--ask-for-approval", CODEX_APPROVAL,
        "--ephemeral",
        task,
    ]
    proc = await asyncio.to_thread(
        subprocess.run, cmd,
        cwd=REPO_PATH, capture_output=True, text=True,
        timeout=ENGINE_TIMEOUT,
    )
    output = proc.stdout or "(no output)"
    if proc.returncode != 0 and proc.stderr:
        stderr_tail = proc.stderr.strip().split("\n")[-5:]
        output += "\n\n⚠️ stderr (tail):\n" + "\n".join(stderr_tail)
    return output


async def run_engine(engine: str, task: str) -> str:
    if engine == "codex":
        return await run_codex(task)
    return await run_claude_code(task)


# ── Git workflow ──────────────────────────────────────────────────────────────

def create_branch(task: str, engine: str) -> str:
    branch = f"{BRANCH_PREFIX}/{engine}/{slugify(task)}-{int(time.time()) % 100000}"
    run_git(["git", "checkout", MAIN_BRANCH])
    run_git(["git", "pull", "--ff-only"])
    run_git(["git", "checkout", "-b", branch])
    return branch


def get_diff() -> str:
    diff = run_git(["git", "diff"])
    diff_staged = run_git(["git", "diff", "--cached"])
    combined = (diff.stdout or "") + (diff_staged.stdout or "")

    untracked = run_git(["git", "ls-files", "--others", "--exclude-standard"])
    if untracked.stdout.strip():
        combined += f"\n\nNew files:\n{untracked.stdout.strip()}"

    return combined.strip() or "(no changes detected)"


async def commit_and_push(branch: str, description: str) -> str:
    run_git(["git", "add", "."])
    run_git(["git", "commit", "-m", f"auto: {description}"])
    push = run_git(["git", "push", "-u", "origin", branch])
    if push.returncode == 0:
        return f"✅ Pushed to `{branch}`"
    return f"❌ Push failed:\n```\n{push.stderr[-500:]}\n```"


async def discard_changes(branch: str) -> None:
    run_git(["git", "checkout", "."])
    run_git(["git", "clean", "-fd"])
    run_git(["git", "checkout", MAIN_BRANCH])
    run_git(["git", "branch", "-D", branch])


# ── Merge / PR operations ────────────────────────────────────────────────────

async def merge_branch(source: str, target: str) -> str:
    """Merge source branch into target locally, then push target."""
    # Make sure target is up to date
    run_git(["git", "checkout", target])
    pull = run_git(["git", "pull", "--ff-only"])
    if pull.returncode != 0:
        # Target might not exist on remote yet (e.g. first time dev branch)
        pass

    merge = run_git(["git", "merge", source, "--no-ff",
                      "-m", f"merge {source} into {target}"])

    if merge.returncode != 0:
        conflict_msg = merge.stdout or merge.stderr or "unknown error"
        # Abort the failed merge
        run_git(["git", "merge", "--abort"])
        return f"❌ Merge conflict merging `{source}` → `{target}`:\n```\n{truncate(conflict_msg, 500)}\n```\nMerge aborted. Resolve manually at your desktop."

    push = run_git(["git", "push", "origin", target])
    if push.returncode != 0:
        return f"❌ Merged locally but push failed:\n```\n{push.stderr[-500:]}\n```"

    return f"✅ Merged `{source}` → `{target}` and pushed."


async def create_pr(source: str, target: str, title: str) -> str:
    """Create a GitHub PR using the gh CLI."""
    if not has_gh_cli():
        return "❌ `gh` CLI not installed. Install it with `sudo apt install gh` and run `gh auth login`."

    result = run_git([
        "gh", "pr", "create",
        "--base", target,
        "--head", source,
        "--title", title,
        "--body", f"Auto-generated PR from Discord bot.\n\nTask: {title}",
    ])

    if result.returncode == 0:
        pr_url = result.stdout.strip()
        return f"✅ PR created: {pr_url}"

    return f"❌ PR creation failed:\n```\n{result.stderr[-500:]}\n```"


# ── Discord event handlers ───────────────────────────────────────────────────

HELP_TEXT = """**Task commands:**
`<task>` — run with default engine ({default})
`claude: <task>` / `cc: <task>` — force Claude Code
`codex: <task>` / `cx: <task>` — force Codex CLI

**After review:**
`yes` / `push` — commit & push to feature branch
`no` / `discard` — discard changes

**Merge commands** (use after pushing):
`merge dev` — merge last pushed branch → dev
`merge main` — merge last pushed branch → main
`merge dev>main` — merge dev → main
`pr dev` — create PR targeting dev
`pr main` — create PR targeting main

**Info:**
`status` — repo branch & working tree
`branches` — list recent branches
`engine` — show engine config
`help` — this message""".format(default=DEFAULT_ENGINE)


@client.event
async def on_ready():
    print(f"🤖 Bot online as {client.user}")
    print(f"   Allowed user : {ALLOWED_USER_ID}")
    print(f"   Repo         : {REPO_PATH}")
    print(f"   Default engine: {DEFAULT_ENGINE}")
    print(f"   Claude model : {CLAUDE_MODEL}")
    print(f"   Codex model  : {CODEX_MODEL}")
    print(f"   gh CLI       : {'available' if has_gh_cli() else 'not found'}")


# Track last pushed branch per channel for merge commands
last_pushed: dict[int, str] = {}


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not is_authorised(message):
        return

    content = message.content.strip()
    if not content:
        return

    ch = message.channel
    lower = content.lower()

    # ── Approval / rejection ──────────────────────────────────────────────
    if lower in ("yes", "approve", "push", "lgtm", "ship it"):
        pending = pending_approvals.pop(ch.id, None)
        if pending:
            await ch.send("⏳ Committing and pushing...")
            result = await commit_and_push(pending["branch"], pending["description"])
            await ch.send(result)
            if "✅" in result:
                last_pushed[ch.id] = pending["branch"]
                await ch.send(
                    f"You can now:\n"
                    f"• `merge dev` — merge → {DEV_BRANCH}\n"
                    f"• `merge main` — merge → {MAIN_BRANCH}\n"
                    f"• `pr dev` / `pr main` — create a PR instead"
                )
            run_git(["git", "checkout", MAIN_BRANCH])
            return
        await ch.send("Nothing pending to approve.")
        return

    if lower in ("no", "reject", "discard", "nah"):
        pending = pending_approvals.pop(ch.id, None)
        if pending:
            await discard_changes(pending["branch"])
            await ch.send(f"🗑️ Changes discarded, back on `{MAIN_BRANCH}`.")
            return
        await ch.send("Nothing pending to discard.")
        return

    # ── Merge commands ────────────────────────────────────────────────────
    if lower.startswith("merge "):
        target_str = lower[6:].strip()

        # "merge dev>main" means merge dev into main
        if ">" in target_str:
            parts = target_str.split(">", 1)
            source = parts[0].strip()
            target = parts[1].strip()
            # Resolve aliases
            source = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(source, source)
            target = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(target, target)
            await ch.send(f"⏳ Merging `{source}` → `{target}`...")
            result = await merge_branch(source, target)
            await ch.send(result)
            return

        # "merge dev" or "merge main" — merge last pushed branch into target
        target = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(target_str, target_str)
        source = last_pushed.get(ch.id)
        if not source:
            await ch.send("No recently pushed branch to merge. Push a feature branch first.")
            return

        await ch.send(f"⏳ Merging `{source}` → `{target}`...")
        result = await merge_branch(source, target)
        await ch.send(result)
        return

    # ── PR commands ───────────────────────────────────────────────────────
    if lower.startswith("pr "):
        target_str = lower[3:].strip()
        target = {"dev": DEV_BRANCH, "main": MAIN_BRANCH}.get(target_str, target_str)
        source = last_pushed.get(ch.id)
        if not source:
            await ch.send("No recently pushed branch. Push a feature branch first.")
            return

        await ch.send(f"⏳ Creating PR: `{source}` → `{target}`...")
        result = await create_pr(source, target, f"auto: {source}")
        await ch.send(result)
        return

    # ── Info commands ─────────────────────────────────────────────────────
    if lower == "help":
        await ch.send(HELP_TEXT)
        return

    if lower == "status":
        st = run_git(["git", "status", "--short"])
        br = current_branch()
        await ch.send(
            f"📍 `{REPO_PATH}`\n"
            f"🌿 `{br}`\n"
            f"```\n{st.stdout.strip() or '(clean)'}\n```"
        )
        return

    if lower == "branches":
        result = run_git(["git", "branch", "--sort=-committerdate", "--format=%(refname:short)"])
        branches = result.stdout.strip().split("\n")[:10]
        listing = "\n".join(f"• `{b}`" for b in branches if b)
        await ch.send(f"**Recent branches:**\n{listing}")
        return

    if lower == "engine":
        await ch.send(
            f"Default engine: **{DEFAULT_ENGINE}**\n"
            f"Claude: `{CLAUDE_MODEL}` · Codex: `{CODEX_MODEL}`\n"
            f"Codex sandbox: `{CODEX_SANDBOX}` · approval: `{CODEX_APPROVAL}`"
        )
        return

    # ── Main task ─────────────────────────────────────────────────────────
    engine, task = parse_engine_and_task(content)
    if not task:
        await ch.send("Give me a task to work on.")
        return

    # Discard any stale pending approval
    stale = pending_approvals.pop(ch.id, None)
    if stale:
        await discard_changes(stale["branch"])
        await ch.send("⚠️ Previous pending changes discarded.\n")

    label = "Claude Code" if engine == "claude" else "Codex CLI"
    await ch.send(f"🧠 **{label}** working on it...\n> {truncate(task, 200)}")

    try:
        branch = create_branch(task, engine)
    except Exception as e:
        await ch.send(f"❌ Branch creation failed: `{e}`")
        return

    try:
        output = await run_engine(engine, task)
    except subprocess.TimeoutExpired:
        await ch.send(f"⏰ {label} timed out ({ENGINE_TIMEOUT}s).")
        await discard_changes(branch)
        return
    except Exception as e:
        await ch.send(f"❌ {label} error: `{e}`")
        await discard_changes(branch)
        return

    await ch.send(f"**{label} says:**\n```\n{truncate(output, 1800)}\n```")

    diff = get_diff()
    await ch.send(f"**Changes on `{branch}`:**\n```diff\n{truncate(diff, 1800)}\n```")

    if "(no changes detected)" in diff:
        await ch.send("No file changes. Branch cleaned up.")
        await discard_changes(branch)
        return

    pending_approvals[ch.id] = {
        "branch": branch,
        "description": task,
        "diff": diff,
        "engine": engine,
    }
    await ch.send(
        "👆 Review the diff.\n"
        "Reply **yes** to commit & push, or **no** to discard."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
