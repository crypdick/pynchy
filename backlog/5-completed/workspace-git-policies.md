# Workspace Git Workflow Policies

**Status: Implemented.** All steps completed. PR workflow uses ambient git/gh credentials. Repo-scoped tokens (when implemented) will thread per-repo `env` through `run_git` and `gh` calls for credential isolation.

**Soft dependency:** [Repo-scoped tokens](../2-planning/repo-scoped-tokens.md) — hardens auth for the PR workflow's push and `gh pr create`. Not blocking: ambient credentials work today.

## Problem

Currently, non-admin workspaces have exactly one git workflow: merge-to-main. When an agent calls `sync_worktree_to_main`, the host rebases the worktree onto main, fast-forward merges, and pushes main to origin. This is fine for trusted agents working on the same repo as the admin, but it's not always the right policy:

- **Merge-to-main** makes sense for trusted automation (code-improver, the admin's own work). Changes land on main immediately.
- **Branch + PR** makes sense for less-trusted or review-gated workflows. The agent pushes to a branch and opens a PR. A human (or deputy agent) reviews before merging.

The choice should be per-workspace, not global. A code-improver workspace might merge directly while an experimental agent opens PRs.

## Design Goals

- Configurable per-workspace via `config.toml`.
- Existing merge-to-main behavior is the default (backward compatible).
- Branch+PR workflow pushes the worktree branch to origin and opens a PR via `gh` CLI.
- The container-side tool (`sync_worktree_to_main`) stays the same — the host decides what "sync" means based on policy.
- Response format is the same regardless of policy (success bool + message string).

## Configuration

Add a `git_policy` field to `WorkspaceConfig`:

```toml
[workspaces.code-improver]
name = "Code Improver"
is_admin = false
repo_access = "owner/repo"
git_policy = "merge-to-main"   # default

[workspaces.experimental-agent]
name = "Experimental Agent"
is_admin = false
repo_access = "owner/repo"
git_policy = "pull-request"
```

**File:** `src/pynchy/config.py`

```python
class WorkspaceConfig(_StrictModel):
    # ...existing fields...
    git_policy: Literal["merge-to-main", "pull-request"] | None = None
    # None → "merge-to-main" (backward compatible default)
```

## Implementation Steps

### Step 1: Config — `git_policy` field

**File:** `src/pynchy/config.py`

Add `git_policy: Literal["merge-to-main", "pull-request"] | None = None` to `WorkspaceConfig`. `None` means merge-to-main (default, backward compatible).

Propagate to `WorkspaceProfile` in `types.py` if needed for runtime access, or resolve at the point of use from config.

### Step 2: Host-side PR workflow — `host_create_pr_from_worktree()`

**File:** `src/pynchy/git_ops/sync.py` (or new file `src/pynchy/git_ops/pr.py`)

New function that implements the branch+PR workflow:

```python
def host_create_pr_from_worktree(
    group_folder: str,
    repo_ctx: RepoContext,
) -> dict[str, Any]:
    """Host-side: push worktree branch to origin and open/update a PR.

    Returns {"success": bool, "message": str}.
    """
    worktree_path = repo_ctx.worktrees_dir / group_folder
    branch_name = f"worktree/{group_folder}"
    main_branch = detect_main_branch(cwd=repo_ctx.root)

    if not worktree_path.exists():
        return {"success": False, "message": f"No worktree found for {group_folder}."}

    # 1. Check for uncommitted changes
    status = run_git("status", "--porcelain", cwd=worktree_path)
    if status.returncode == 0 and status.stdout.strip():
        return {
            "success": False,
            "message": (
                "You have uncommitted changes. Commit all changes first, "
                "then call sync_worktree_to_main again."
            ),
        }

    # 2. Check if there are commits ahead of main
    count = run_git("rev-list", f"{main_branch}..{branch_name}", "--count", cwd=repo_ctx.root)
    try:
        ahead = int(count.stdout.strip())
    except (ValueError, TypeError):
        return {"success": False, "message": f"Failed to check commits: {count.stderr.strip()}"}
    if ahead == 0:
        return {"success": True, "message": "Already up to date — no commits to push."}

    # 3. Push the worktree branch to origin
    # Use the repo-scoped token env (from repo-scoped-tokens plan)
    env = git_env_with_token(repo_ctx.slug)
    push = run_git("push", "-u", "origin", branch_name, "--force-with-lease", cwd=repo_ctx.root, env=env)
    if push.returncode != 0:
        return {"success": False, "message": f"Push failed: {push.stderr.strip()}"}

    # 4. Check if a PR already exists for this branch
    pr_check = subprocess.run(
        ["gh", "pr", "view", branch_name, "--json", "url", "--jq", ".url"],
        cwd=str(repo_ctx.root),
        capture_output=True,
        text=True,
        timeout=30,
    )

    if pr_check.returncode == 0 and pr_check.stdout.strip():
        # PR exists — it auto-updates when we push
        pr_url = pr_check.stdout.strip()
        return {
            "success": True,
            "message": f"Pushed {ahead} commit(s) to {branch_name}. PR updated: {pr_url}",
        }

    # 5. Create a new PR
    # Build title from latest commit message
    title_result = run_git("log", "-1", "--format=%s", cwd=worktree_path)
    pr_title = title_result.stdout.strip() if title_result.returncode == 0 else f"Changes from {group_folder}"

    # Build body from commit log
    body_result = run_git(
        "log", f"{main_branch}..{branch_name}", "--format=- %s", cwd=repo_ctx.root
    )
    pr_body = f"Automated PR from workspace `{group_folder}`.\n\n### Commits\n{body_result.stdout.strip()}"

    pr_create = subprocess.run(
        [
            "gh", "pr", "create",
            "--base", main_branch,
            "--head", branch_name,
            "--title", pr_title,
            "--body", pr_body,
        ],
        cwd=str(repo_ctx.root),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,  # needs GH_TOKEN for auth
    )

    if pr_create.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Pushed {ahead} commit(s) to {branch_name}, but PR creation failed: "
                f"{pr_create.stderr.strip()}"
            ),
        }

    pr_url = pr_create.stdout.strip()
    return {
        "success": True,
        "message": f"Pushed {ahead} commit(s) and opened PR: {pr_url}",
    }
```

**Key behaviors:**
- **Push with `--force-with-lease`:** The worktree branch is rebased at startup, so subsequent pushes need force-push. `--force-with-lease` is safer than `--force`.
- **Idempotent:** If a PR already exists for the branch, just push (PR auto-updates). No duplicate PRs.
- **PR title from commit:** Uses the latest commit message as the PR title. For multi-commit pushes, the body lists all commits.
- **`gh` CLI auth:** The `gh` CLI uses `GH_TOKEN` from the environment. The host needs the per-repo token in its env when calling `gh pr create`.

### Step 3: Route IPC handler based on policy

**File:** `src/pynchy/ipc/_handlers_lifecycle.py`

In `_handle_sync_worktree_to_main()`, read the workspace's `git_policy` and route:

```python
async def _handle_sync_worktree_to_main(request, ipc_state):
    group_folder = request["groupFolder"]

    # Resolve policy
    s = get_settings()
    ws_cfg = s.workspaces.get(group_folder)
    policy = (ws_cfg.git_policy if ws_cfg and ws_cfg.git_policy else "merge-to-main")

    repo_ctx = resolve_repo_for_group(group_folder)
    if repo_ctx is None:
        result = {"success": False, "message": "No repo_access configured."}
    elif policy == "pull-request":
        result = await asyncio.to_thread(host_create_pr_from_worktree, group_folder, repo_ctx)
    else:
        result = host_sync_worktree(group_folder, repo_ctx)

    # Write response (existing code)
    write_ipc_response(response_path, result)

    # Only notify other worktrees on merge-to-main (PRs don't change main)
    if policy == "merge-to-main" and result.get("success"):
        await host_notify_worktree_updates(group_folder, deps, repo_ctx)
```

### Step 4: Update background merge behavior

**File:** `src/pynchy/git_ops/worktree.py` — `background_merge_worktree()`

Currently called after every session (message handler, session end, scheduler). For `pull-request` policy, background merge should push the branch + update the PR instead of merging to main.

```python
def background_merge_worktree(group: object) -> None:
    from pynchy.config import get_settings

    folder: str = group.folder
    repo_ctx = resolve_repo_for_group(folder)
    if repo_ctx is None:
        return

    s = get_settings()
    ws_cfg = s.workspaces.get(folder)
    policy = ws_cfg.git_policy if ws_cfg and ws_cfg.git_policy else "merge-to-main"

    if policy == "pull-request":
        create_background_task(
            asyncio.to_thread(host_create_pr_from_worktree, folder, repo_ctx),
            name=f"worktree-pr-{folder}",
        )
    else:
        create_background_task(
            asyncio.to_thread(merge_and_push_worktree, folder, repo_ctx),
            name=f"worktree-merge-{folder}",
        )
```

### Step 5: Update the container-side tool description

**File:** `container/agent_runner/src/agent_runner/agent_tools/_tools_lifecycle.py`

The tool is currently called `sync_worktree_to_main` with description "Merge your worktree into main and push to origin." This description is inaccurate for the PR policy.

Two options:

**Option A: Dynamic description via settings.** The container already receives `settings.json` with workspace metadata. Add `git_policy` to the settings and adjust the tool description at registration time.

**Option B: Generic description.** Rename/update the description to be policy-neutral:

```python
# Tool definition
name = "sync_worktree_to_main"  # keep name for backward compat
description = (
    "Publish your committed changes. Depending on workspace policy, this either "
    "merges into main and pushes, or pushes to a branch and opens/updates a PR. "
    "Commit all changes first."
)
```

**Recommendation:** Option B is simpler and avoids coupling the tool definition to config plumbing. The response message already tells the agent what happened (merged vs PR URL).

### Step 6: Worktree rebase behavior for PR policy

For merge-to-main, the startup reconciler rebases diverged worktree branches onto main. This is important because it enables ff-merges.

For PR policy, rebasing onto main is still useful (keeps the PR clean, reduces conflicts at merge time), but it's not strictly required. The PR branch can diverge from main and GitHub will show the diff correctly.

**Recommendation:** Keep the rebase behavior for both policies. It keeps PRs up-to-date and reduces merge conflicts. The `--force-with-lease` push in Step 2 handles the rewritten history.

### Step 7: Documentation

Update `docs/usage/worktrees.md` and `docs/architecture/security.md` to describe the two policies and when to use each.

## Files Changed

| File | Change |
|------|--------|
| `src/pynchy/config.py` | Add `git_policy` to `WorkspaceConfig` |
| `src/pynchy/git_ops/sync.py` (or new `pr.py`) | Add `host_create_pr_from_worktree()` |
| `src/pynchy/ipc/_handlers_lifecycle.py` | Route IPC based on `git_policy` |
| `src/pynchy/git_ops/worktree.py` | Policy-aware `background_merge_worktree()` |
| `container/agent_runner/src/agent_runner/agent_tools/_tools_lifecycle.py` | Update tool description |
| `docs/usage/worktrees.md` | Document both policies |
| `docs/architecture/security.md` | Note policy options in privilege table |

## Testing

1. **Unit: `host_create_pr_from_worktree()`** — mock `gh pr view`, `gh pr create`, `run_git`; verify push, PR check, PR creation, idempotent update.
2. **Unit: IPC routing** — verify merge-to-main policy calls `host_sync_worktree`, pull-request policy calls `host_create_pr_from_worktree`.
3. **Unit: `background_merge_worktree()` policy dispatch** — verify correct function called per policy.
4. **Integration: full PR workflow** — requires a test repo; agent commits, calls sync, verify branch pushed and PR created.
5. **Integration: subsequent push updates PR** — push more commits, verify same PR updated (no duplicates).

## Migration

Fully backward compatible. `git_policy` defaults to `None` which resolves to `merge-to-main`. Existing workspaces behave identically. New workspaces can opt into `pull-request`.
