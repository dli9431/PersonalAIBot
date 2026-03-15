# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Discord bot that delegates coding tasks to AI CLI tools (Claude Code and OpenAI Codex) with automated git workflow management. Designed for mobile-first use via Discord. Single-file Python application (`bot.py`, ~1873 lines).

## Running

```bash
pip install discord.py python-dotenv
cp env.example .env   # then fill in required values
python bot.py
# or use start.sh which activates the venv automatically
```

External CLIs required: `@anthropic-ai/claude-code` and `@openai/codex` (npm global installs), `gh` (GitHub CLI), Node.js 22+.

No build step, test suite, or linter is configured.

## Architecture

**bot.py** is the entire application, structured as:

1. **Config & State** (~lines 29-110) — Environment variables via `python-dotenv`. `GIT_PROJECTS` list of (label, path) tuples for multi-repo support. Per-channel state: `active_sessions`, `last_pushed`, `channel_cwd`, `branch_listing`. Stop events and running procs tracked for cancellation.

2. **Helpers** (~lines 113-493) — Auth check, slugify, `run_git(cmd, path)` with optional cwd override, `resolve_project`, `resolve_branch` (N ref lookup), `get_diff`/`get_diff_stat`, structured `review` parsing/formatting helpers (major changes with before/after/why), branch/protection helpers, ANSI stripping, image download helpers.

3. **Login Helpers** (~lines 495-579) — `login_codex()` and `login_claude()` run the respective CLI auth flows, streaming output to Discord. Both use a `_login_lock` dict to prevent concurrent logins.

4. **Engine Runners** (~lines 581-767) — `run_claude_code()` and `run_codex()` execute CLIs as subprocesses with `cwd` param. `run_engine()` wraps both with auto-continue on timeout (up to 3 retries, `MAX_AUTO_CONTINUES=3`). `_run_with_live_output()` streams output to a Discord message, refreshing every `STATUS_REFRESH=5` seconds.

5. **Git Workflow** (~lines 769-863) — `create_branch` (`{BRANCH_PREFIX}/{engine}/{slug}-{timestamp % 100000}`), `auto_commit` (WIP save after each turn), `commit_and_push`, `discard_changes`, `merge_branch` (with conflict detection and branch cleanup), `create_pr` via `gh`.

6. **Discord Handlers** (~lines 865-1838) — `ensure_pinned_help` pins/updates help embeds. `on_ready`/`on_resumed` pin help to all accessible channels and send restart confirmation. `on_message` routes all commands.

## Key Patterns

- **Dual-engine**: Prefix tasks with `claude:`/`cc:` or `codex:`/`cx:`/`openai:` to pick an engine, or use `DEFAULT_ENGINE` from `.env`.
- **Iterative sessions**: Follow-up messages continue with `--resume` / `exec resume --last`. Sessions have phases: `"working"` → `"review"` → `"merge_target"` (when no dev branch) → cleared. Turn counter incremented each run, reflected in WIP commit messages.
- **Review flow**: `review`/`done` shows structured major changes (before/after/why) → `yes`/`push`/`approve`/`lgtm`/`ship it` to commit+push+merge, or `no`/`discard`/`reject`/`nah` to abandon.
- **No-change cleanup**: If the engine makes no file changes, the feature branch is deleted and no session is started.
- **Multi-repo**: `GIT_PROJECTS` env var registers additional repos. `cwd <n>` switches the active repo per channel. All git ops accept an optional `path` param.
- **Branch switching**: `branch switch <branch|N>` (or `switch <branch|N>`) mid-session auto-commits and checks out the target branch. `N` refs are populated by the `branches` command via `branch_listing` dict (cached per channel).
- **Switchable cwd mid-session**: `cwd <n>` during an active session auto-commits current work and updates `session["cwd"]`.
- **Protected branches**: Stored in `.bot_state.json`, survive restarts. Default: `MAIN_BRANCH` and `DEV_BRANCH`. Managed via `branch protect` commands.
- **State persistence**: `.bot_state.json` stores protected branches and last active channel's cwd/branch. Restored on startup.
- **Image attachments**: Saved to `/tmp/botimages/` and passed to both engines (Claude gets file paths, Codex gets `--image` flag).
- **Run cancellation**: `stop` terminates the active subprocess (graceful kill, 5s timeout fallback).
- **Tool sandboxing**: Claude Code runs with `CLAUDE_ALLOWED_TOOLS` / `CLAUDE_DENIED_TOOLS` from env. Codex runs in workspace-write mode with network off (configured via `~/.codex/config.toml`).
- **All git work happens on feature branches**, never directly on main/dev.
- **Config uses `.env`** with sensible defaults. See `env.example` for all variables.

## Discord Commands (handled in on_message)

**Task execution:** plain text, `claude: <task>`, `cc: <task>`, `codex: <task>`, `cx: <task>`, `openai: <task>`
**Session control:** `stop`, `review`, `done`, `yes`/`push`/`approve`/`lgtm`/`ship it`, `no`/`discard`/`reject`/`nah`, `abort`, `skip`, `diff`, `undo`
**Branch nav:** `branch switch <branch|N>`, `switch <branch|N>`, `cwd [n]`, `branches`, `branch delete <name|N> [local|remote|both] [force]`, `branch protect [list|add|remove|clear|reset]`
**Git:** `merge <target>`, `merge src>tgt`, `merge src into tgt`, `pr <target>`, `pull [branch|N]`
**Multi-repo:** `repos`, `repo <n> status|diff|review|commit [msg]|push|branches`
**Recovery:** `recover`, `recover <id>`, `recover drop <id>`
**Config:** `claude models`, `codex models` — list available (numbered); `claude model <n|name>`, `cc model <n|name>`, `codex model <n|name>`, `cx model <n|name>`, `engine claude`, `engine codex`, `engine claude model <n|name> [reasoning <n|level>]`, `engine codex model <n|name> [reasoning <n|level>]`, `engine`
**Login:** `claude login`, `codex login`, `openai login`, `login both`
**System:** `status`, `help`, `restart`
