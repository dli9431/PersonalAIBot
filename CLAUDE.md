# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Discord bot that delegates coding tasks to AI CLI tools (Claude Code and OpenAI Codex) with automated git workflow management. Designed for mobile-first use via Discord. Single-file Python application (`bot.py`, ~1440 lines).

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

1. **Config & State** (~lines 29-97) — Environment variables via `python-dotenv`. `GIT_PROJECTS` list of (label, path) tuples for multi-repo support. Per-channel state: `active_sessions`, `last_pushed`, `channel_cwd`, `branch_listing`.

2. **Helpers** (~lines 99-278) — Auth check, slugify, `run_git(cmd, path)` with optional cwd override, `resolve_project`, `resolve_branch` (#N ref lookup), `get_diff`/`get_diff_stat` (compares feature branch vs base, not just working tree), `branch_merged_status`.

3. **Login Helpers** (~lines 280-363) — `login_claude()` and `login_codex()` run the respective CLI auth flows, streaming output to Discord.

4. **Engine Runners** (~lines 365-522) — `run_claude_code()` and `run_codex()` execute CLIs as subprocesses with `cwd` param. `run_engine()` wraps both with auto-continue on timeout (up to 3 retries). `_run_with_live_output()` streams output to a Discord message.

5. **Git Workflow** (~lines 524-613) — `create_branch` (`{BRANCH_PREFIX}/{engine}/{slug}-{timestamp}`), `auto_commit` (WIP save after each turn), `commit_and_push`, `discard_changes`, `merge_branch` (with conflict detection and branch cleanup), `create_pr` via `gh`.

6. **Discord Handlers** (~lines 615-1435) — `ensure_pinned_help` pins/updates help embeds. `on_ready`/`on_resumed` pin help to all accessible channels and send restart confirmation. `on_message` routes all commands.

## Key Patterns

- **Dual-engine**: Prefix tasks with `claude:`/`cc:` or `codex:`/`cx:` to pick an engine, or use `DEFAULT_ENGINE` from `.env`.
- **Iterative sessions**: Follow-up messages continue with `--resume` / `exec resume --last`. Sessions have phases: `"working"` → `"review"` → `"merge_target"` (when no dev branch) → cleared.
- **Review flow**: `done` → diff shown → `yes`/`push` to commit+push+merge, or `no`/`discard` to abandon.
- **No-change cleanup**: If the engine makes no file changes, the feature branch is deleted and no session is started.
- **Multi-repo**: `GIT_PROJECTS` env var registers additional repos. `cwd <n>` switches the active repo per channel. All git ops (`run_git`, `get_diff`, `create_branch`, etc.) accept an optional `path` param.
- **Branch switching**: `switch <branch|#N>` mid-session auto-commits and checks out the target branch. `#N` refs are populated by the `branches` command via `branch_listing` dict.
- **Switchable cwd mid-session**: `cwd <n>` during an active session auto-commits current work and updates `session["cwd"]`.
- **All git work happens on feature branches**, never directly on main/dev.
- **Config uses `.env`** with sensible defaults. See `env.example` for all variables.

## Discord Commands (handled in on_message)

**Task execution:** plain text, `claude: <task>`, `cc: <task>`, `codex: <task>`, `cx: <task>`
**Session:** `done`, `yes`/`push`, `no`/`discard`, `abort`, `skip`, `diff`, `undo`
**Branch nav:** `switch <branch|#N>`, `cwd [n]`, `branches`, `branch delete <name|#N> [local|remote] [force]`
**Git:** `merge <target>`, `merge src>tgt`, `merge src into tgt`, `pr <target>`, `pull [branch]`
**Multi-repo:** `repos`, `repo <n> status|diff|commit|push|branches`
**Recovery:** `recover`, `recover <id>`, `recover drop <id>`
**Config:** `claude model <name>`, `codex model <name>`, `engine`
**Login:** `claude login`, `codex login`, `login both`
**System:** `status`, `help`, `restart`
