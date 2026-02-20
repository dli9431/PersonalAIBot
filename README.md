# Discord → Claude Code / Codex CLI → Git Bridge

Self-hosted Discord bot: send coding tasks from your phone, run them through
Claude Code or Codex CLI on your local machine (Linux, or WSL on Windows),
review the diff, push, and merge — all from Discord.

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

# Claude Code settings
CLAUDE_MODEL=sonnet
CLAUDE_ALLOWED_TOOLS=Read Edit Write Grep Glob LS Bash(git\ diff) Bash(git\ status)
CLAUDE_DENIED_TOOLS=Bash(rm\ *) Bash(sudo\ *) Bash(curl\ *) Bash(wget\ *) WebFetch
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
codex: refactor the database queries
cc: fix the CSS on the navbar
cx: add error handling to the API routes
openai: add a loading spinner to the form
```

The bot creates a feature branch, runs the engine, and streams its output.

### Iterate

Send follow-up messages freely — the engine keeps session context via `--resume`.

```
also add client-side validation
make the error messages more descriptive
```

### Review and push

```
done      → shows full diff + push prompt
yes       → commit, push & merge to dev/main
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
| `<task>` | Run with the default engine |
| `claude: <task>` / `cc: <task>` | Run with Claude Code |
| `codex: <task>` / `cx: <task>` | Run with Codex CLI |

### During a session

| Command | Description |
|---------|-------------|
| `<follow-up>` | Continue with same engine and context |
| `diff` | Peek at current changes |
| `undo` | Revert the last engine run |
| `branch switch <branch\|N>` | Save & switch to another branch |
| `cwd <n>` | Save & switch active repo (from GIT_PROJECTS) |
| `abort` | Discard all changes immediately |

### Ending a session

| Command | Description |
|---------|-------------|
| `done` | Show full diff and prompt for push |
| `yes` / `push` | Commit, push & merge |
| `no` / `discard` | Discard all changes |
| `skip` | Commit & push, skip the merge step |

### Git

| Command | Description |
|---------|-------------|
| `merge <target>` | Merge current/last-pushed branch into target |
| `merge src>tgt` | Explicit source and target |
| `merge src into tgt` | Same as above |
| `pr <target>` | Open a GitHub pull request |
| `pull [branch]` | Pull latest from remote (defaults to current branch) |

### Branches

| Command | Description |
|---------|-------------|
| `branches` | List recent branches (assigns `N` references) |
| `branch switch <branch\|N>` | Switch branch in active session |
| `branch delete <name\|N> [local\|remote] [force]` | Delete a branch (checks if merged first) |
| `branch protect [list\|add\|remove\|clear\|reset]` | Manage protected branches (blocks deletion) |

### Multi-repo

| Command | Description |
|---------|-------------|
| `repos` | List all configured repos |
| `cwd` | Show active repo |
| `cwd <n>` | Switch active repo |
| `repo <n> status` | Git status for repo N |
| `repo <n> diff` | Diff for repo N |
| `repo <n> commit [msg]` | Commit staged changes in repo N |
| `repo <n> push` | Push repo N |
| `repo <n> branches` | List branches in repo N |

### Recovery

| Command | Description |
|---------|-------------|
| `recover` | List orphaned feature branches |
| `recover <id>` | Resume an orphaned branch as a session |
| `recover drop <id>` | Delete an orphaned branch |

### Config & Info

| Command | Description |
|---------|-------------|
| `engine` | Show current engine config and all available models |
| `claude models` / `cc models` | List available Claude models |
| `codex models` / `cx models` | List available Codex models |
| `claude model <name>` | Switch Claude model (e.g. `opus`, `sonnet`, `haiku`) |
| `codex model <name>` | Switch Codex model |
| `status` | Current branch and working tree |
| `help` | Show command reference (pinned) |

### Login & System

| Command | Description |
|---------|-------------|
| `claude login` | Re-authenticate Claude Code |
| `codex login` | Re-authenticate Codex CLI |
| `login both` | Re-authenticate both |
| `restart` | Restart the bot process |

---

## Typical Workflow

```
You:   add a dark mode toggle to the settings page
Bot:   🧠 Claude Code working on it...
Bot:   [streams Claude's output]
Bot:   Changes detected. Reply done to review, or keep going.

You:   done
Bot:   [shows git diff]
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
- **Feature branches only** — the bot never commits directly to main or dev; you always review a diff first
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
- [ ] All git work on feature branches, never main directly
- [ ] Review the diff before saying `yes`
- [ ] SSH keys for git auth (not HTTPS with stored password)
- [ ] (WSL) Repos under WSL filesystem (`/home/...`), not `/mnt/c/`

---

## Disclaimer

This project is vibe-coded — built iteratively with AI assistance and shared as-is. It works well as a personal tool but comes with no guarantees. Use at your own discretion. The MIT license applies: no warranty, no liability, no support obligations.

If it breaks your repo, deletes your code, or causes other chaos — that's on you. Review the diff before saying `yes`.

---

## License

MIT — see [LICENSE](LICENSE)
