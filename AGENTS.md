# AGENTS.md

This repository is a self-hosted Discord bot that turns Discord messages into coding runs executed by Claude Code, Codex CLI, or Kimi Code CLI, then manages the resulting git workflow.

## Project Snapshot

- Main application: `bot.py` (single-file Python app, currently about 4.8k lines)
- Runtime: Python 3.11+ (`tomllib` is imported from the standard library)
- Python deps: `discord.py`, `python-dotenv`
- Shell entrypoint: `start.sh`
- User-facing docs: `README.md`
- Agent-specific docs: `CLAUDE.md`, this file

The bot is designed for Linux and WSL2. It expects the external CLIs `claude`, `codex`, `kimi`, and optionally `gh` to be installed on the host machine.

## Run And Setup

Typical local setup:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp env.example .env
bash start.sh
```

Required environment variables live in `.env`:

- `DISCORD_TOKEN`
- `ALLOWED_USER_ID`
- `REPO_PATH`

Important optional configuration:

- Branching: `BRANCH_PREFIX`, `MAIN_BRANCH`, `DEV_BRANCH`, `PROTECTED_BRANCHES`
- Engine defaults: `DEFAULT_ENGINE`, `ENGINE_TIMEOUT`
- Context persistence: `CONTEXT_MAX_CHARS`, `PLAN_CONTEXT_MAX_CHARS`
- Claude runtime: `CLAUDE_MODEL`, `CLAUDE_REASONING_EFFORT`, `CLAUDE_ALLOWED_TOOLS`, `CLAUDE_DENIED_TOOLS`
- Codex runtime: `CODEX_MODEL`, `CODEX_REASONING_EFFORT`
- Kimi runtime: `KIMI_MODEL`, `KIMI_REASONING_EFFORT`
- Extra repos: `GIT_PROJECTS`

Codex sandbox and approval settings are read from `~/.codex/config.toml`, not from `.env`. See `codex-config-example.toml`.

## Architecture

`bot.py` is organized into a few large sections:

1. Configuration and in-memory state for sessions, running subprocesses, per-channel cwd, per-channel runtime config, and usage tracking.
2. Helper functions for git operations, branch resolution, state persistence, plan/resume context, model selection, trust checks, diff/review formatting, and image handling.
3. Login helpers for Claude, Codex, and Kimi CLI auth flows.
4. Engine runners that stream live output to Discord, support timeout auto-resume, and capture token usage.
5. Git workflow helpers for feature branch creation, WIP auto-commits, push, merge, discard, PR creation, and worktree lifecycle.
6. Discord handlers, mostly inside `on_message`, which implement the command surface.

## Important Runtime Behavior

- Sessions run in per-channel git worktrees under `<repo>/.worktrees/ch-<channel_id>`.
- Feature branches use the form `{BRANCH_PREFIX}/{engine}/{slug}-{timestamp}`.
- The bot never intends to work directly on `main` or `dev`; it creates feature branches first.
- Follow-up messages continue the current engine session with Claude resume, Kimi `-c` continue, or Codex resume.
- While a run is active, `add:` / `queue:` stores follow-up instructions and resumes automatically after the current turn.
- Planning mode is persisted per Discord channel. `plan:` stores plan context, `plan: do` executes it later.
- `.bot_state.json` stores protected branches, runtime config, usage totals, saved channel cwd/branch, queued follow-ups, resume context, and saved plan context.
- Runtime config is layered: global defaults from `.env`, then optional per-channel overrides persisted in `.bot_state.json`.
- Token usage is tracked both per run and cumulatively across runs.

## Current Command Surface

The bot supports more than simple task execution. Important command groups:

- Task execution: plain text, `claude:`, `codex:`, `cc:`, `cx:`, `openai:`, `kimi:`, `km:`
- Planning: `plan: <task>`, `plan: do`, `plan show`, `plan clear`
- Session control: follow-up text, `stop`, `diff`, `review`, `done`, `yes`, `skip`, `no`, `undo`, `abort`
- Queueing during active runs: `add: <instruction>`, `queue: <instruction>`
- Repo management: `repos`, `cwd`, `repo <n> ...`
- Branch management: `branches`, `switch`, `branch switch`, `branch delete`, `branch protect`, `pull`
- Recovery and workflow: `recover`, `recover drop`, `merge`, `pr`
- Runtime config: `engine`, `engine global`, `claude models`, `codex models`, `kimi models`, `claude model`, `codex model`, `kimi model`, reasoning commands
- Diagnostics: `status`, `usage`, `doctor`, `help`, `restart`

Engine logins: `claude login`, `codex login`, `kimi login` (`login both` covers Claude + Codex only).

If you change command handling in `on_message`, update the pinned help text in `bot.py`, `README.md`, and `CLAUDE.md` as needed.

## Editing Guidance

- Keep changes localized when possible; most behavior lives in `bot.py`.
- Treat `.bot_state.json`, `.env`, and credentials as local runtime state, not source-controlled application logic.
- Preserve the worktree-based isolation model. Repo-path changes often need to consider canonical repo paths vs worktree paths.
- When modifying session flow, check how it interacts with:
  - `active_sessions`
  - `active_run_contexts`
  - `stop_events`
  - resume context and queued follow-ups
  - saved plan context
  - cleanup in `_end_session()`
- When modifying engine config behavior, check both global runtime config persistence and per-channel overrides.

## Validation

There is no automated test suite in this repository right now.

Useful manual checks:

```bash
python3 -m py_compile bot.py
python3 bot.py
```

For behavior changes, validate at least the affected Discord command flow and the corresponding git/worktree behavior.
