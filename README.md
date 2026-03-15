# Discord → Claude Code / Codex CLI → Git Bridge

Self-hosted Discord bot: send coding tasks from your phone, run them through
Claude Code or Codex CLI on your local machine (Linux, or WSL on Windows),
review major changes, push, and merge — all from Discord.

> **Note:** This project replicates one specific workflow from GitHub Copilot —
> the agent chat mode where you describe a task and it makes code changes for you.
> It does **not** replace Copilot entirely, and it still requires an active
> [Anthropic](https://anthropic.com) or [OpenAI](https://openai.com) subscription
> to use the underlying CLIs. This is a personal side project built for fun.
> GitHub, please don't sue me.

---

## Quick Setup

### 1. Prerequisites

#### Native Linux

```bash
# Node.js via nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash
source ~/.bashrc && nvm install 22

# AI CLIs
npm install -g @anthropic-ai/claude-code @openai/codex
claude        # authenticate (browser), then Ctrl+C
codex         # authenticate (browser), then /exit

# GitHub CLI (for PR creation)
sudo apt update && sudo apt install gh
gh auth login  # GitHub.com → SSH → web browser
```

#### WSL2 (Windows)

Same steps as above, run inside your WSL2 terminal. Keep your repos under the WSL filesystem (`/home/...`), not `/mnt/c/`.


### 2. Discord Bot

1. Go to https://discord.com/developers/applications → **New Application**
2. **Bot** tab → **Reset Token** → copy the token
3. Enable **Message Content Intent** (under Privileged Gateway Intents)
4. **OAuth2 → URL Generator**: scope `bot`, permissions:
   - Send Messages
   - Read Message History
   - Manage Messages _(required for pinning help)_
5. Invite the bot to a **private server with only you**
6. Enable **Developer Mode** in Discord settings → right-click your username → **Copy User ID**

### 3. Bot Setup

```bash
git clone https://github.com/dli9431/PersonalAIBot.git
cd PersonalAIBot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp env.example .env
nano .env   # fill in required values (see below)
```

### 4. Configure `.env`

```env
# Required
DISCORD_TOKEN=your-bot-token-here
ALLOWED_USER_ID=123456789012345678   # your Discord user ID
REPO_PATH=/home/you/code/my-app     # absolute path to your main project

# Optional: additional repos the bot can work in
# GIT_PROJECTS=MyApp:../my-app,OtherRepo:/home/you/other-repo

# Branch settings
BRANCH_PREFIX=auto
MAIN_BRANCH=main
DEV_BRANCH=dev
PROTECTED_BRANCHES=main,dev

# Engine defaults
DEFAULT_ENGINE=claude    # or codex
ENGINE_TIMEOUT=300
CONTEXT_MAX_CHARS=4000   # max chars saved for timeout resume context
PLAN_CONTEXT_MAX_CHARS=12000  # max chars saved for plan + plan do/plan: do context

# Claude Code settings
CLAUDE_MODEL=sonnet
# Optional: low | medium | high (unset = CLI default)
CLAUDE_REASONING_EFFORT=
CLAUDE_ALLOWED_TOOLS=Read Edit Write Grep Glob LS Bash(git\ diff) Bash(git\ status)
CLAUDE_DENIED_TOOLS=Bash(rm\ *) Bash(sudo\ *) Bash(curl\ *) Bash(wget\ *) WebFetch

# Codex CLI settings
CODEX_MODEL=gpt-5.3-codex
# Optional: low | medium | high | xhigh (unset = CLI default)
CODEX_REASONING_EFFORT=
```

> **Note:** Use absolute paths (e.g. `/home/you/code`) not `~/code` in `.env`.

### 5. Run

```bash
bash start.sh
```

Or in a persistent tmux session so it survives terminal close:

```bash
tmux new -s bot
bash start.sh
# Ctrl+B, D to detach
```

---

## Usage

### Send a task

```
add input validation to the signup form
claude: write tests for the auth module
claude code: improve caching in the API client
codex: refactor the database queries
cc: fix the CSS on the navbar
cx: add error handling to the API routes
openai: add a loading spinner to the form
```

The bot creates a feature branch, runs the engine, and streams its output.

You can also plan first, then execute:

```
plan: add retry logic to the payment webhook flow
plan: do
```

`plan:` runs in planning mode with this channel's default engine/model and saves context to disk.
Successive `plan:` commands in the same repo/engine extend the saved context instead of replacing it.
`plan: do` (or `plan do`, optionally with extra instructions) executes the last saved plan context for this
channel, then clears it.
Use `plan show` to inspect saved plan context and `plan clear` to remove it manually.

### Iterate

Send follow-up messages freely — the engine keeps session context (`claude --continue`, `codex exec resume --last`).
If a run times out, the bot saves a short resume-context snapshot and injects it on the next engine resume.
If the run still times out after all automatic retries, the bot also saves an unfinished session snapshot you can reopen with `resume` later (`resume show` to inspect it).
Clear either snapshot with `context clear` if needed.
If you need to append work while a turn is still running, send `add: <instruction>` (or `queue:`). The bot saves run context, queues the instruction, then resumes automatically.

```
also add client-side validation
make the error messages more descriptive
```

### Check usage and limits

```
usage
```

Shows all-time token usage per engine, live remaining usage limits for Claude/Codex (best effort),
and current session token counts when available.

### Review and push

```
review    → detailed major-change review (before / after / why)
done      → descriptive per-file summary + push prompt
yes       → commit + push, then merge to dev (or choose merge target)
no        → discard all changes
abort     → discard immediately (any time)
```

### After pushing

```
merge dev             → merge current/last-pushed branch → dev
merge main            → merge into main
merge dev>main        → merge dev → main (e.g. for releases)
merge dev into main   → same as above
pr dev                → open a GitHub PR targeting dev
pr main               → open a GitHub PR targeting main
```

---

## Command Reference

### Starting a session

| Command | Description |
|---------|-------------|
| `<task>` | Run with this channel's default engine |
| `claude: <task>` / `cc: <task>` / `claude code: <task>` | Run with Claude Code |
| `codex: <task>` / `cx: <task>` / `openai: <task>` | Run with Codex CLI |
| `plan: <task>` | Planning mode with this channel's default engine/model; saves plan context |
| `plan: do [extra instructions]` / `plan do [extra instructions]` | Execute saved plan context with this channel's default engine/model, then clear it |

### During a session

| Command | Description |
|---------|-------------|
| `<follow-up>` | Continue with the same engine and the session's model/reasoning snapshot |
| `stop` | Cancel the currently running engine turn |
| `add: <instruction>` / `queue: <instruction>` | Queue extra instructions during an in-progress run; bot resumes automatically after the current turn |
| `diff` | Quick raw peek at current changes |
| `review` | Detailed major-change review (before/after/why) |
| `undo` | Discard uncommitted working-tree changes in the active session |
| `switch <branch\|N>` | Switch branches (auto-commit if in session) |
| `cwd <n>` | Save & switch active repo (from GIT_PROJECTS) |
| `resume` | Reopen the saved unfinished timeout session for this channel |
| `resume show` | Inspect the saved unfinished timeout snapshot without reopening it |
| `context clear` | Forget saved timeout/resume context, unfinished timeout snapshot, and queued follow-ups (`resume clear` / `clear context` aliases) |
| `plan show` | Show saved plan context for this channel |
| `plan clear` | Clear saved plan context without executing it (`clear plan` alias) |
| `abort` | Discard all changes immediately |

### Ending a session

| Command | Description |
|---------|-------------|
| `done` | Show a descriptive per-file summary and prompt for push |
| `yes` / `push` | Commit + push, then merge to `DEV_BRANCH` if it exists (otherwise asks for merge target) |
| `no` / `discard` | Discard all changes |
| `skip` | Commit & push, skip the merge step |

### Git

| Command | Description |
|---------|-------------|
| `merge <target>` | Merge current/last-pushed branch into target |
| `merge src>tgt` | Explicit source and target |
| `merge src into tgt` | Same as above |
| `pr <target>` | Open a GitHub pull request |
| `pull [branch\|N]` | Pull latest from remote (supports numbered branch refs from `branches`; defaults to resolved base: dev/main if present, else origin/HEAD or a fallback branch) |

### Branches

| Command | Description |
|---------|-------------|
| `branches` | List recent branches (assigns `N` references) |
| `switch <branch\|N>` / `branch switch <branch\|N>` | Switch branches (auto-commit if in session) |
| `branch delete <name\|N> [local\|remote\|both] [force]` / `branch del ...` | Delete a branch (checks if merged first, unless `force`) |
| `branch protect [list\|add\|remove\|clear\|reset]` | Manage protected branches (blocks deletion) |

### Multi-repo

| Command | Description |
|---------|-------------|
| `repos` | List all configured repos |
| `cwd` | Show active repo |
| `cwd <n>` | Switch active repo |
| `repo <n> status` | Git status for repo N |
| `repo <n> diff` | Diff for repo N |
| `repo <n> review` | Detailed major-change review for repo N |
| `repo <n> commit [msg]` | Commit staged changes in repo N |
| `repo <n> push` | Push repo N |
| `repo <n> branches` | List branches in repo N |

### Recovery

| Command | Description |
|---------|-------------|
| `resume` | Reopen the saved unfinished timeout session for this channel |
| `resume show` | Show the saved unfinished timeout snapshot |
| `recover` | List orphaned feature branches |
| `recover <id>` | Resume an orphaned branch as a session |
| `recover drop <id>` | Delete an orphaned branch |

### Config & Info

| Command | Description |
|---------|-------------|
| `engine` | Show this channel's engine/model/reasoning config (plus global defaults) and available models |
| `engine global` | Show global default engine/model/reasoning config |
| `engine claude` / `engine codex` | Set this channel's default engine only (keeps this channel's current model for that engine) |
| `engine global claude` / `engine global codex` | Set global default engine only |
| `claude models` / `cc models` | List available Claude models (numbered) |
| `codex models` / `cx models` | List available Codex models (numbered) |
| `claude model <n\|name>` | Set this channel's Claude model by number or name (e.g. `1`, `opus`, `sonnet`) |
| `codex model <n\|name>` | Set this channel's Codex model by number or name |
| `engine claude model <n\|name> [reasoning <n\|level>]` | Set this channel default engine to Claude, choose model by number or name, and optionally set reasoning in the same command |
| `engine codex model <n\|name> [reasoning <n\|level>]` | Set this channel default engine to Codex, choose model by number or name, and optionally set reasoning in the same command |
| `engine global claude model <n\|name> [reasoning <n\|level>]` | Set global default engine to Claude, choose model by number or name, and optionally set reasoning in the same command |
| `engine global codex model <n\|name> [reasoning <n\|level>]` | Set global default engine to Codex, choose model by number or name, and optionally set reasoning in the same command |
| `claude reasoning [n\|level]` | View/set this channel's Claude reasoning by number or name (`1=low`, `2=medium`, `3=high`, `4=default`) |
| `codex reasoning [n\|level]` / `cx reasoning ...` / `openai reasoning ...` | View/set this channel's Codex reasoning by number or name (`1=low`, `2=medium`, `3=high`, `4=xhigh`, `5=default`) |
| `engine claude reasoning <n\|level>` | Set this channel default engine to Claude and set reasoning effort |
| `engine codex reasoning <n\|level>` | Set this channel default engine to Codex and set reasoning effort |
| `engine global claude reasoning <n\|level>` | Set global default engine to Claude and set reasoning effort |
| `engine global codex reasoning <n\|level>` | Set global default engine to Codex and set reasoning effort |
| `model <n\|name>` | Set model for this channel's default engine by number or name |
| `default model <n\|name>` | Alias for `model <n\|name>` |
| `reasoning [n\|level]` | View/set reasoning effort for this channel's default engine |
| `default reasoning [n\|level]` | Alias for `reasoning [n\|level]` |
| `status` | Current branch and working tree |
| `usage` | Show cumulative token usage per engine (all-time runs, input/output/cache tokens), plus live remaining usage-limit status for Claude/Codex (best effort), and current session token count when available |
| `doctor` | Run diagnostics for SSH, CLI auth, trust, and repo health |
| `help` | Show command reference (pinned) |

Global defaults and channel-scoped engine/model/reasoning selections are persisted in `.bot_state.json` and restored on restart.

### Login & System

| Command | Description |
|---------|-------------|
| `claude login` / `cc login` | Re-authenticate Claude Code |
| `codex login` / `cx login` / `openai login` | Re-authenticate Codex CLI |
| `login both` | Re-authenticate both |
| `restart` | Restart the bot process |

---

## Typical Workflow

```
You:   add a dark mode toggle to the settings page
Bot:   🧠 Claude Code working on it...
Bot:   [streams Claude's output]
Bot:   Changes detected. Reply review to inspect major changes, or keep going.

You:   done
Bot:   [shows descriptive per-file summary]
Bot:   Reply yes to commit & push, no to discard.

You:   yes
Bot:   ✅ Pushed to auto/claude/add-a-dark-mode-toggle-38291
Bot:   ✅ Merged → dev

# Later, release dev to main:
You:   merge dev>main
Bot:   ✅ Merged dev → main and pushed.
```

---

## Multi-repo Setup

Add extra repos to `.env`:

```env
GIT_PROJECTS=MyApp:../my-app,OtherRepo:/home/you/other-repo
```

- Project 1 is always `REPO_PATH`
- Additional entries start at project 2
- Paths can be absolute or relative to `REPO_PATH`
- Use `repos` to list them, `cwd <n>` to switch

The bot can even manage its own code — add `PersonalAIBot:/path/to/PersonalAIBot` to `GIT_PROJECTS` and use `cwd 2` (or whatever number) to switch to it.

---

## Running in Background

### tmux (recommended)

```bash
tmux new -s bot
bash /path/to/PersonalAIBot/start.sh
# Ctrl+B, D to detach
# tmux attach -t bot  to return
```

### Keep WSL alive after closing terminal (Windows only)

Create `%USERPROFILE%\.wslconfig` on Windows:

```ini
[wsl2]
vmIdleTimeout=-1
```

Or add a Windows Task Scheduler task on login:

```
Program:   wsl
Arguments: -d Ubuntu -- tmux new-session -d -s bot "cd /home/you/PersonalAIBot && bash start.sh"
```

---

## Security

### What's built in

- **ALLOWED_USER_ID gate** — every message is checked against your Discord user ID; no one else can trigger anything
- **Tool deny list** — Claude Code is blocked from `rm`, `sudo`, `curl`, `wget`, and `WebFetch` by default
- **Codex sandbox** — `workspace-write` mode with network off, configured via `~/.codex/config.toml` (see `codex-config-example.toml`)
- **Session tasks use feature branches** — task/follow-up runs branch off your base and show a final change summary before push/merge; direct commits to other branches only happen if you explicitly run manual git commands (for example `repo <n> commit`)
- **No secrets in code** — all tokens loaded from `.env` at runtime, never hardcoded, `.env` gitignored
- **SSH auth for git** — no stored HTTPS passwords

### Limitations to be aware of

- **Claude Code has broad file access** — it can read and edit any file in your repo; the deny list blocks shell commands but doesn't prevent it from reading sensitive file contents
- **Discord is the attack surface** — if your Discord account is compromised, so is this bot; enable 2FA on your Discord account
- **No containerization** — the bot runs with your user's OS permissions; a malicious `CLAUDE.md` in an untrusted repo could influence Claude's behavior
- **No rate limiting** — the user ID check prevents execution, but the bot will process every message it receives

### Setup checklist

- [ ] Private Discord server, only you
- [ ] 2FA enabled on your Discord account
- [ ] Bot checks `ALLOWED_USER_ID` on every message
- [ ] `.env` in `.gitignore`
- [ ] Claude Code: `rm`, `sudo`, `curl`, `wget` denied via `CLAUDE_DENIED_TOOLS`
- [ ] Codex: copy `codex-config-example.toml` → `~/.codex/config.toml` for workspace-write sandbox + no network
- [ ] Session task work stays on feature branches (avoid direct commits on `main`/`dev` unless intentional)
- [ ] Run `review` for detail or `done` for a descriptive summary before saying `yes`
- [ ] SSH keys for git auth (not HTTPS with stored password)
- [ ] (WSL) Repos under WSL filesystem (`/home/...`), not `/mnt/c/`

---

## Disclaimer

This project is vibe-coded — built iteratively with AI assistance and shared as-is. It works well as a personal tool but comes with no guarantees. Use at your own discretion. The MIT license applies: no warranty, no liability, no support obligations.

If it breaks your repo, deletes your code, or causes other chaos — that's on you. Run `review` for detail or `done` for a descriptive summary before saying `yes`.

---

## License

MIT — see [LICENSE](LICENSE)
