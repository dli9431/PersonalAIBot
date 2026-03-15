# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

This repo is a self-hosted Discord bot that delegates coding tasks to Claude Code or OpenAI Codex CLI, streams results back into Discord, and manages the surrounding git workflow.

Current shape of the project:

- Main app: `bot.py` (single-file Python application, currently about 4.8k lines)
- Entry script: `start.sh`
- Python deps: `discord.py`, `python-dotenv`
- Runtime expectation: Python 3.11+
- Primary user documentation: `README.md`

There is no build step, test suite, formatter, or linter configured in the repo.

## Running

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp env.example .env
python3 bot.py
```

Or:

```bash
bash start.sh
```

Required external tools:

- `claude`
- `codex`
- `gh` for PR creation

The project is intended for Linux or WSL2. Codex sandbox settings come from `~/.codex/config.toml`; see `codex-config-example.toml`.

## Configuration

Required `.env` values:

- `DISCORD_TOKEN`
- `ALLOWED_USER_ID`
- `REPO_PATH`

Important optional values:

- Branch settings: `BRANCH_PREFIX`, `MAIN_BRANCH`, `DEV_BRANCH`, `PROTECTED_BRANCHES`
- Engine defaults: `DEFAULT_ENGINE`, `ENGINE_TIMEOUT`
- Context persistence: `CONTEXT_MAX_CHARS`, `PLAN_CONTEXT_MAX_CHARS`
- Claude settings: `CLAUDE_MODEL`, `CLAUDE_REASONING_EFFORT`, `CLAUDE_ALLOWED_TOOLS`, `CLAUDE_DENIED_TOOLS`
- Codex settings: `CODEX_MODEL`, `CODEX_REASONING_EFFORT`
- Multi-repo support: `GIT_PROJECTS`
- State file override: `BOT_STATE_FILE`

Runtime config is persisted in `.bot_state.json` and can diverge from `.env` because the bot supports global and per-channel overrides from Discord commands.

## Architecture

`bot.py` is organized into these major areas:

1. Configuration and process/session state.
2. Helpers for git, branch resolution, state persistence, runtime config, usage tracking, plan context, resume context, review formatting, and image downloads.
3. Model discovery and login helpers for Claude and Codex.
4. Engine runners:
   - `run_claude_code()`
   - `run_codex()`
   - `run_engine()`
5. Git workflow helpers:
   - `create_branch()`
   - `auto_commit()`
   - `commit_and_push()`
   - `discard_changes()`
   - `merge_branch()`
   - `create_pr()`
6. Worktree helpers:
   - `ensure_worktree()`
   - `remove_worktree()`
   - `_end_session()`
7. Discord handlers, mainly `on_message()`, plus pinned help maintenance and slash `/help`.

## Key Runtime Behavior

- Each Discord channel gets its own git worktree under `<repo>/.worktrees/ch-<channel_id>`.
- Feature branches are created as `{BRANCH_PREFIX}/{engine}/{slug}-{timestamp}`.
- Active sessions are tracked in `active_sessions`; running processes and cancellation state are tracked separately.
- Follow-up prompts continue the prior engine session using Claude resume or Codex resume.
- If a run times out, the bot saves resume context and automatically retries up to `MAX_AUTO_CONTINUES = 3`.
- While a run is still active, `add:` and `queue:` save follow-up work for automatic resume after the current turn.
- Planning mode is persisted per channel:
  - `plan: <task>` stores planning output without editing files.
  - `plan: do` later executes the saved plan in a new feature branch.
- The bot records token usage per run and cumulative totals in `.bot_state.json`.
- Protected branches are persisted and enforced beyond just `main` and `dev`.
- Image attachments are downloaded to `/tmp/botimages/` and passed through to the engine.

## State And Persistence

`.bot_state.json` may contain:

- `protected_branches`
- `runtime_config`
- `channel_runtime_configs`
- `usage_stats`
- `channels` and `last_active_channel`
- saved resume contexts
- queued follow-up commands
- saved planning contexts

Be careful when changing serialization formats because there is migration-free persistence logic built around tolerant reads and overwrites.

## Current Command Surface

Task execution:

- Plain text uses the channel default engine
- `claude: <task>`, `cc: <task>`, `claude code: <task>`
- `codex: <task>`, `cx: <task>`, `openai: <task>`

Planning and recovery:

- `plan: <task>`
- `plan: do [extra instructions]`
- `plan show`
- `plan clear`
- `context clear`
- `recover`
- `recover <id>`
- `recover drop <id>`

Session flow:

- Follow-up plain text
- `stop`
- `add: <instruction>` / `queue: <instruction>`
- `diff`
- `review`
- `done`
- `yes` / `approve` / `push` / `lgtm` / `ship it`
- `skip`
- `no` / `discard` / `reject` / `nah`
- `undo`
- `abort`

Repo and branch flow:

- `repos`
- `cwd` / `cwd <n>`
- `repo <n> status|diff|review|commit [msg]|push|branches`
- `branches`
- `switch <branch|N>`
- `branch switch <branch|N>`
- `branch delete|del <name|N> [local|remote|both] [force]`
- `branch protect [list|add|remove|clear|reset]`
- `pull [branch|N]`
- `merge <target>`
- `merge src>tgt`
- `merge src into tgt`
- `pr <target>`

Runtime configuration and diagnostics:

- `engine`
- `engine global`
- `claude models`
- `codex models`
- `claude model <n|name>`
- `codex model <n|name>`
- `claude reasoning [n|level]`
- `codex reasoning [n|level]`
- `reasoning [n|level]`
- `model <n|name>`
- `usage`
- `status`
- `doctor`
- `help`
- `restart`
- `claude login`, `codex login`, `openai login`, `login both`

## Editing Notes

- Most feature work lands in `bot.py`; keep related command/help/docs changes synchronized.
- If you change command behavior, update:
  - the help text in `bot.py`
  - `README.md`
  - this file if the workflow summary changes
- Preserve the distinction between canonical repo paths and per-channel worktree paths.
- Session cleanup is easy to break; check how your change interacts with `_end_session()`, branch checkout fallback, and worktree removal.
- Runtime config changes must consider both global defaults and per-channel overrides.
- Avoid assuming the README is fully current; verify against `bot.py`.

## Validation

There is no automated test suite in this repo. Minimum safe validation after code changes:

```bash
python3 -m py_compile bot.py
```

For workflow changes, also manually exercise the affected Discord command path.
