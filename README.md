# Discord → Claude Code / Codex CLI → Git Bridge

Self-hosted bot: send coding tasks from your phone via Discord, run them
through Claude Code or Codex CLI on your local machine (WSL2), review the
diff, push, and merge — all from your phone.

---

## Quick Setup

Assumes you already have WSL2, Node 22, both CLIs authenticated, and a
repo cloned to `~/code/`. If not, see the full setup steps below.

```bash
cd ~/claude-discord-bridge
cp .env.example .env
nano .env                    # fill in your values
pip install discord.py python-dotenv
python bot.py
```

---

## Full Setup

### 1. WSL2 + Node + CLIs

```bash
# Inside WSL
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash
source ~/.bashrc && nvm install 22
npm install -g @anthropic-ai/claude-code @openai/codex
claude        # auth via browser, then Ctrl+C
codex         # auth via browser, then /exit
```

### 2. gh CLI (needed for PR creation)

```bash
sudo apt update
sudo apt install gh
gh auth login
# Select: GitHub.com → HTTPS → Login with a web browser
```

### 3. Discord Bot

1. https://discord.com/developers/applications → New Application
2. Bot tab → Reset Token → copy it
3. Enable **Message Content Intent**
4. OAuth2 → URL Generator: scope `bot`, permissions `Send Messages` + `Read Message History`
5. Invite to a **private server with only you**
6. Enable Developer Mode in Discord → right-click your name → Copy User ID

### 4. Configure and Run

```bash
cp .env.example .env
nano .env
# Set: DISCORD_TOKEN, ALLOWED_USER_ID, REPO_PATH, DEV_BRANCH
python bot.py
```

---

## Usage (from your phone)

### Send a task

```
add input validation to the signup form
claude: write tests for the auth module
codex: refactor the database queries
cc: fix the CSS on the navbar
cx: add error handling to the API routes
```

The bot creates a feature branch, runs the engine, and sends you the diff.

### Review and push

```
yes        → commit & push the feature branch
no         → discard everything
```

### Merge after pushing

```
merge dev           → merge your feature branch into dev
merge main          → merge your feature branch into main
merge dev>main      → merge dev into main (e.g. for releases)
```

### Or create a PR instead

```
pr dev              → open a PR targeting dev
pr main             → open a PR targeting main
```

### Info commands

```
status              → current branch & working tree
branches            → list 10 most recent branches
engine              → show engine configuration
help                → command reference
```

---

## Typical Workflow

```
You (phone):  add a dark mode toggle to the settings page
Bot:          🧠 Claude Code working on it...
Bot:          [shows Claude's response]
Bot:          [shows git diff]
Bot:          Reply yes to commit & push, or no to discard.

You:          yes
Bot:          ✅ Pushed to auto/claude/add-a-dark-mode-toggle-38291
Bot:          You can now: merge dev, merge main, or pr dev / pr main

You:          merge dev
Bot:          ✅ Merged auto/claude/add-a-dark-mode-toggle-38291 → dev and pushed.

# Later, when dev is ready for release:
You:          merge dev>main
Bot:          ✅ Merged dev → main and pushed.
```

---

## Running in Background

### tmux (simplest)

```bash
tmux new -s bot
python bot.py
# Ctrl+B, D to detach
```

### Keep WSL alive after closing terminal

Create `%USERPROFILE%\.wslconfig` on Windows:

```ini
[wsl2]
vmIdleTimeout=-1
```

Or add a Task Scheduler task on Windows login:

```
Program:   wsl
Arguments: -d Ubuntu -- tmux new-session -d -s bot "cd ~/claude-discord-bridge && python bot.py"
```

---

## Security Checklist

- [ ] Private Discord server, only you
- [ ] Bot checks ALLOWED_USER_ID on every message
- [ ] .env in .gitignore
- [ ] Claude Code: rm, sudo, curl, wget denied
- [ ] Codex: workspace-write sandbox, network OFF
- [ ] Pushes go to feature branches, never main directly
- [ ] You review the diff before approving
- [ ] SSH keys for git auth
- [ ] Repo under WSL filesystem, not /mnt/c/
