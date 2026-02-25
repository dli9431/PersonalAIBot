# Plan: Add Git Worktrees for Per-Channel Isolation on Same Repo

## Goal

Allow multiple Discord channels to work simultaneously on the **same repository** without git conflicts by giving each channel its own git worktree. Today, `channel_cwd` maps channels to repo paths, but two channels targeting the same repo share one working tree — concurrent branch checkouts and commits collide. Worktrees solve this by giving each channel an independent working directory with its own checked-out branch.

## Design

### Core Concept

- When a channel starts a session on a repo that already has a worktree used by another channel, create a **new git worktree** for that channel.
- Worktrees live under `<repo>/.worktrees/<channel_id>/` (gitignored).
- `channel_cwd` will point to the worktree path instead of the bare repo path.
- The "primary" repo path stays as-is for the first channel that uses it, or we can give every channel its own worktree for consistency (simpler logic).

### Approach: Lazy Worktree Per Channel

Each channel gets its own worktree the first time it starts a session. The worktree is created from the repo's base branch. This is the simplest approach — always isolate, never share.

**Worktree path format:** `<repo_path>/.worktrees/ch-<channel_id>/`

## Steps

### 1. Add worktree management helpers (~4 functions)

Location: new section in bot.py after the git workflow section (~line 1513)

```python
def _worktree_base(repo_path: str) -> pathlib.Path:
    """Return the .worktrees/ directory for a repo."""
    return pathlib.Path(repo_path) / ".worktrees"

def _worktree_path(repo_path: str, channel_id: int) -> str:
    """Return the worktree path for a specific channel."""
    return str(_worktree_base(repo_path) / f"ch-{channel_id}")

def ensure_worktree(repo_path: str, channel_id: int, branch: str | None = None) -> str:
    """Create or reuse a worktree for this channel. Returns worktree path."""
    wt_path = _worktree_path(repo_path, channel_id)
    if pathlib.Path(wt_path).exists():
        return wt_path
    base = branch or _resolve_checkout_branch(repo_path)
    pathlib.Path(wt_path).parent.mkdir(parents=True, exist_ok=True)
    result = run_git(["git", "worktree", "add", wt_path, base], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"Worktree creation failed: {result.stderr}")
    return wt_path

def remove_worktree(repo_path: str, channel_id: int) -> None:
    """Remove a channel's worktree after session ends."""
    wt_path = _worktree_path(repo_path, channel_id)
    if pathlib.Path(wt_path).exists():
        run_git(["git", "worktree", "remove", wt_path, "--force"], repo_path)

def get_main_repo(worktree_path: str) -> str | None:
    """Given a possible worktree path, return the main repo path, or None."""
    # Check if path is inside a .worktrees/ dir
    p = pathlib.Path(worktree_path)
    if p.parent.name == ".worktrees":
        return str(p.parent.parent)
    return None
```

### 2. Integrate worktree creation into session start

In the new-task handler (~line 3060), after resolving `cwd` but before `create_branch`:

- Determine the "canonical" repo path (strip any existing worktree suffix to find the real repo).
- Call `ensure_worktree(canonical_repo, ch.id)` to get an isolated working directory.
- Use that worktree path as `cwd` for the rest of the session.
- Store both `"repo"` (canonical path) and `"cwd"` (worktree path) in the session dict.

### 3. Update `create_branch` to work in worktrees

Worktrees share the branch namespace with the main repo. `git checkout -b` works fine in a worktree — the new branch is visible across all worktrees. No change needed to branch creation logic, just ensure `path` points to the worktree.

**One key constraint:** Two worktrees cannot have the same branch checked out simultaneously. Our branch names include timestamps so this won't collide for feature branches. But the base branch checkout in `create_branch` (line 1436: `git checkout base`) will fail if another worktree has that branch. Fix: use `git worktree add <path> -b <new-branch> <base>` instead, or detach HEAD before checkout, or skip the base checkout and use `--no-checkout` + reset.

**Best fix:** Modify `create_branch` to detect worktree context and create the branch directly:
```python
# In worktree: create branch from base without checking out base first
run_git(["git", "branch", branch, base], path)
run_git(["git", "checkout", branch], path)
```
Or even simpler: `git checkout -b branch base` (creates branch from base ref without needing to be on base first). This already works — just replace the two-step (checkout base → checkout -b branch) with one step: `git checkout -b branch base`.

### 4. Update `cwd <n>` command to handle worktrees

When switching repos via `cwd <n>`:
- If there's an active session, auto-commit and then clean up the old worktree (or leave it for potential recovery).
- Create a new worktree for the new repo if needed.
- Update `channel_cwd` to the worktree path.

### 5. Update cleanup on session end

When a session ends (merge, discard, abort, skip):
- After branch cleanup, call `remove_worktree(repo_path, ch.id)`.
- Reset `channel_cwd[ch.id]` back to the canonical repo path (or remove it).

### 6. Update `discard_changes` to handle worktree removal

After discarding, if the cwd is a worktree:
- Checkout base branch in the worktree.
- Remove the worktree via `git worktree remove`.

### 7. Update `merge_branch` to handle worktree context

Merging from a worktree should work on the main repo (since branches are shared). After merge + branch deletion:
- Remove the worktree.
- Update channel cwd.

### 8. Update state persistence

- `record_state`: store both `cwd` (worktree path) and `repo` (canonical path).
- `restore_state`: on restart, worktrees may still exist on disk. Re-validate them. If missing, recreate or fall back to canonical path.

### 9. Add `.worktrees/` to `.gitignore`

Ensure worktree directories aren't tracked.

### 10. Update `recover` command

Orphaned worktrees should be discoverable. `recover` should also list branches in worktrees. Add `git worktree list` output to diagnostics.

## Files

| File | Changes |
|---|---|
| `bot.py` | Add worktree helpers, modify session start/end, update `create_branch`, `discard_changes`, `merge_branch`, `cwd` command, state persistence |
| `.gitignore` | Add `.worktrees/` pattern |

## Validation

1. **Single channel, single repo** — should work exactly as before (worktree created transparently).
2. **Two channels, same repo** — each gets its own worktree; concurrent sessions don't conflict.
3. **Session end cleanup** — worktree removed after merge/discard/abort.
4. **Bot restart** — worktrees survive on disk; state file has enough info to reconnect.
5. **`cwd` switching** — moving between repos mid-session cleans up old worktree, creates new one.
6. **Branch operations** — `branches`, `branch switch`, `branch delete` still work within worktree context.
7. **`recover`** — can find sessions in orphaned worktrees.

## Risks

| Risk | Mitigation |
|---|---|
| **Worktree disk usage** — each worktree is a full checkout | Aggressive cleanup on session end; worktrees are cheap (they share `.git` objects) |
| **Stale worktrees on crash** — bot crashes mid-session, worktree left behind | `restore_state` validates worktrees on startup; `git worktree prune` in startup routine |
| **Branch checkout conflict** — `create_branch` checks out base, but another worktree may have it | Fix `create_branch` to use `git checkout -b <new> <base>` (one step, no need to be on base) |
| **Engine CLI trust/config** — Claude Code `.claude/settings.json` trust is per-directory; worktree may not be trusted | Worktrees are subdirs of the repo, so `allowedDirectories` patterns should cover them. May need `doctor` update. |
| **Merge conflicts between worktrees** — two channels edit same files | Same risk as any concurrent development; merge step will catch conflicts as it does today |
| **Complexity** — adds ~100-150 lines of worktree management | Contained in helper functions; existing code paths get minimal changes (just cwd swapped to worktree path) |
