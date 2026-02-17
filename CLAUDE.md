# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Discord bot that delegates coding tasks to AI CLI tools (Claude Code and OpenAI Codex) with automated git workflow management. Designed for mobile-first use via Discord. Single-file Python application (`bot.py`).

## Running

```bash
pip install discord.py python-dotenv
cp .env.example .env   # then fill in required values
python bot.py
```

External CLIs required: `@anthropic-ai/claude-code` and `@openai/codex` (npm global installs), `gh` (GitHub CLI), Node.js 22+.

No build step, test suite, or linter is configured.

## Architecture

**bot.py** is the entire application (~520 lines), structured as:

1. **Config & State** (top) — Environment variables loaded via `python-dotenv`. Per-channel session state stored in `active_sessions` dict (branch, engine, task description, turn count, phase).

2. **Helpers** (~lines 68-136) — Authorization check, slug generation for branch names, git command runner with timeout, text truncation for Discord's 2000-char limit.

3. **Engine Runners** (~lines 140-203) — `run_claude_code()` and `run_codex()` execute the respective CLIs as subprocesses. Both support `--resume` for iterative multi-turn sessions.

4. **Git Workflow** (~lines 208-265) — Branch creation (`{BRANCH_PREFIX}/{engine}/{slug}-{timestamp}`), commit/push, merge with conflict detection, PR creation via `gh`.

5. **Discord Message Handler** (`on_message`, ~lines 305-512) — Command routing, session lifecycle, task execution. Session phases flow: `"working"` → `"review"` → cleared.

## Key Patterns

- **Dual-engine**: Users prefix tasks with `claude:`/`cc:` or `codex:`/`cx:` to pick an engine, or use `DEFAULT_ENGINE` from `.env`.
- **Iterative sessions**: After the first task, follow-up messages in the same channel continue the session with `--resume` / `exec resume --last`.
- **Review flow**: `done` → bot shows diff → user says `yes`/`push` to commit+push, or `no`/`discard` to abandon. `abort` emergency-discards mid-session.
- **All git work happens on feature branches**, never directly on main/dev.
- **Config uses `.env`** with sensible defaults. See `.env.example` for all variables. Separate config examples exist for Claude Code permissions (`claude-settings-example.json`) and Codex (`codex-config-example.toml`).

## Discord Commands (handled in on_message)

Task execution: plain text, `claude: <task>`, `codex: <task>`
Session: `done`, `yes`/`push`, `no`/`discard`, `abort`, `diff`, `undo`
Git: `merge [branch]`, `merge src>target`, `pr [target]`
Info: `status`, `branches`, `engine`, `help`
