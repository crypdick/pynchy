# Repo-Scoped Tokens for Non-Admin Workspaces

**Backlog item:** `ensure_repo_cloned()` (`repo.py:64`) uses bare `https://github.com/{slug}` with no auth — private repos fail. Non-admin workspaces with `repo_access` should automatically get repo-scoped GitHub tokens instead of the host's broad `gh_token`.

## Problem

Two related issues:

1. **Private repos can't be cloned.** `ensure_repo_cloned()` runs `git clone https://github.com/{slug}` with no credentials. This works for public repos but silently fails for private ones at startup.

2. **No scoped git access for non-admin containers.** Currently, non-admin containers get zero git credentials — they can't fetch, pull, or push directly. All remote operations route through `host_sync_worktree()` IPC. If we simply pass the host's broad `gh_token`, a compromised container could access every repo the token reaches.

## Design Goals

- Private repos clone successfully at startup.
- Non-admin containers get a git token scoped to **only** their designated repo.
- A compromised container's token cannot reach other repos.
- Host-side sync (`fetch`, `push`) uses per-repo tokens instead of relying on ambient `gh` CLI auth.
- No manual per-container setup — token injection is automatic based on workspace config.
- Backward-compatible: public repos with no token configured still work.

## Mechanism Choice

### Phase 1: Per-repo tokens in config (recommended starting point)

Add a `token` field to `RepoConfig`. The user creates a [GitHub fine-grained PAT](https://docs.github.com/en/authentication/managing-commit-signing-verification/about-commit-signature-verification) scoped to a single repo and puts it in `config.toml` (which is `.gitignored`) or `.env`.

**Pros:** Zero infrastructure, explicit and auditable, each token independently rotatable, works today.
**Cons:** Manual token creation per repo. Fine-grained PATs expire (max 1 year) and need manual rotation.

```toml
# config.toml (gitignored)
[repos."crypdick/other-project"]
token = "github_pat_..."   # fine-grained PAT scoped to this repo
```

### Phase 2: GitHub App (future — auto-generated tokens)

For true auto-generation, a GitHub App can mint short-lived installation tokens scoped to installed repos via API. Requires one-time App creation + install, but then pynchy generates tokens on demand with no manual rotation.

Phase 2 is additive — the same `RepoConfig.token` field becomes the fallback when the App isn't configured. The App just automates what the user does manually in Phase 1.

This plan covers Phase 1 only. Phase 2 can be a separate plan if/when the manual approach becomes friction.

## Implementation Steps

### Step 1: Config — add `RepoConfig.token`

**File:** `src/pynchy/config.py`

```python
class RepoConfig(_StrictModel):
    path: str | None = None
    token: SecretStr | None = None  # repo-scoped GitHub token (fine-grained PAT)
    # ...existing validator...
```

The `token` field is `SecretStr` so it's masked in logs. It's `Optional` because:
- Public repos don't need a token.
- The pynchy repo itself (admin workspace) uses the existing `gh_token` / `gh` CLI.

**Resolution order** for a repo's git credentials:

1. `repos."owner/repo".token` — explicit per-repo token (highest priority)
2. `secrets.gh_token` — host's broad token (fallback for repos without a scoped token)
3. `gh auth token` — auto-discovered from `gh` CLI (lowest priority)

This order means: if you configure a scoped token, it's always used. If you don't, the existing behavior (broad token) applies.

Add a helper function in `repo.py`:

```python
def get_repo_token(slug: str) -> str | None:
    """Resolve the git token for a repo, walking the fallback chain."""
    s = get_settings()
    repo_cfg = s.repos.get(slug)
    if repo_cfg and repo_cfg.token:
        return repo_cfg.token.get_secret_value()
    if s.secrets.gh_token:
        return s.secrets.gh_token.get_secret_value()
    return _read_gh_token()  # imported from _credentials.py or duplicated
```

### Step 2: Fix host-side cloning — `ensure_repo_cloned()`

**File:** `src/pynchy/git_ops/repo.py`

Change the clone URL to include credentials when available:

```python
def ensure_repo_cloned(repo_ctx: RepoContext) -> bool:
    if repo_ctx.root.exists():
        return True

    repo_ctx.root.parent.mkdir(parents=True, exist_ok=True)

    token = get_repo_token(repo_ctx.slug)
    if token:
        clone_url = f"https://x-access-token:{token}@github.com/{repo_ctx.slug}"
    else:
        clone_url = f"https://github.com/{repo_ctx.slug}"

    logger.info("Cloning repo", slug=repo_ctx.slug, dest=str(repo_ctx.root))
    result = subprocess.run(
        ["git", "clone", clone_url, str(repo_ctx.root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Sanitize stderr to avoid leaking tokens in logs
        stderr = _sanitize_token(result.stderr, token)
        logger.error("Failed to clone repo", slug=repo_ctx.slug, stderr=stderr)
        return False

    # After cloning, set the remote URL to the bare form (no embedded token).
    # This prevents the token from persisting in .git/config. Future
    # fetch/push operations will use the credential helper or env token.
    subprocess.run(
        ["git", "remote", "set-url", "origin", f"https://github.com/{repo_ctx.slug}"],
        cwd=str(repo_ctx.root),
        capture_output=True,
    )
    logger.info("Cloned repo", slug=repo_ctx.slug)
    return True
```

**Important:** After cloning, reset the remote URL to the bare form so the token doesn't persist in `.git/config`. Subsequent operations use the credential helper (Step 3).

Also add `_sanitize_token()` to strip tokens from log output:

```python
def _sanitize_token(text: str, token: str | None) -> str:
    if token and token in text:
        return text.replace(token, "***")
    return text
```

### Step 3: Host-side git credential helper

All host-side git operations (`fetch`, `push`, `ls-remote`) in `sync.py` and `worktree.py` currently rely on ambient `gh` CLI credentials. Switch them to use per-repo tokens explicitly.

**Approach:** Set `GIT_ASKPASS` to a script that serves the token for the right repo. This is cleaner than embedding tokens in URLs because:
- Token never appears in `.git/config`
- Token never appears in `git remote -v` output
- Works for all git operations transparently

**File:** `src/pynchy/git_ops/utils.py` (or new file `src/pynchy/git_ops/credentials.py`)

```python
import os
import stat
import tempfile

def git_env_with_token(slug: str) -> dict[str, str] | None:
    """Build env dict with GIT_ASKPASS for a repo's token.

    Returns None if no token is available (fall back to ambient credentials).
    """
    token = get_repo_token(slug)
    if not token:
        return None

    # Write a temporary askpass script that echoes the token.
    # GIT_ASKPASS is called with a prompt argument; we ignore it and
    # always return the token (git uses it as the password).
    askpass = _get_or_create_askpass_script(token)
    env = os.environ.copy()
    env["GIT_ASKPASS"] = str(askpass)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never prompt interactively
    return env
```

Then update `run_git()` in `utils.py` to accept an optional `env` parameter, and thread it through from callers that know the repo slug.

**Alternative (simpler):** Instead of `GIT_ASKPASS`, use `git -c credential.helper='!f() { echo password={token}; }' ...` inline. But this leaks the token in `/proc` and process listings. The askpass script approach is safer.

**Simplest safe approach**: Write a per-repo askpass script to `data/credentials/{owner}/{repo}/askpass.sh` at startup (alongside `ensure_repo_cloned`). The script is `chmod 700`, owned by the pynchy user. It echoes the token. This avoids temp files and race conditions.

### Step 4: Container credential injection

**File:** `src/pynchy/container_runner/_credentials.py`

For non-admin containers with `repo_access`, inject the **scoped** token:

```python
def _write_env_file(*, is_admin: bool, group_folder: str) -> Path | None:
    # ...existing code...

    # GH_TOKEN — admin gets the broad token, non-admin gets repo-scoped token
    if is_admin:
        if s.secrets.gh_token:
            env_vars["GH_TOKEN"] = s.secrets.gh_token.get_secret_value()
        elif gh_token := _read_gh_token():
            env_vars["GH_TOKEN"] = gh_token
    else:
        # Non-admin: inject repo-scoped token if this workspace has repo_access
        ws_cfg = s.workspaces.get(group_folder)
        if ws_cfg and ws_cfg.repo_access:
            repo_cfg = s.repos.get(ws_cfg.repo_access)
            if repo_cfg and repo_cfg.token:
                env_vars["GH_TOKEN"] = repo_cfg.token.get_secret_value()

    # ...rest of existing code...
```

**Key security property:** Non-admin containers only get the token for their specific repo. If `repos."owner/repo".token` is a fine-grained PAT scoped to that repo, the container cannot use it to access anything else.

**What about containers with no `repo_access`?** They continue to get no `GH_TOKEN`, exactly as today.

**What about the `GH_TOKEN` variable name?** We reuse `GH_TOKEN` (not a new variable) because:
- Claude Code and `gh` CLI both check `GH_TOKEN`
- The container agent already knows to use `GH_TOKEN` for git operations
- The scoping comes from the token itself, not the variable name

### Step 5: Configure git credentials inside containers

The container needs to know how to use the `GH_TOKEN` for git operations. Currently the admin container has `GH_TOKEN` and `gh` CLI respects it. For non-admin containers, we need a git credential helper.

**File:** `container/entrypoint.sh` (or equivalent setup script)

Add a conditional credential helper setup:

```bash
# If GH_TOKEN is set, configure git to use it as the password for github.com
if [ -n "$GH_TOKEN" ]; then
    git config --global credential.https://github.com.helper \
        '!f() { echo "protocol=https"; echo "host=github.com"; echo "username=x-access-token"; echo "password=$GH_TOKEN"; }; f'
fi
```

This configures git inside the container to use the injected token for any `github.com` operation. Since the token is repo-scoped, it only works for the designated repo even though the credential helper responds to all `github.com` URLs.

### Step 6: Update host-side sync operations

Thread the per-repo token through all host-side git operations.

**Files:**
- `src/pynchy/git_ops/sync.py` — `host_sync_worktree()`, `_host_update_main()`, `_host_get_origin_main_sha()`
- `src/pynchy/git_ops/worktree.py` — `ensure_worktree()`, `_sync_existing_worktree()`
- `src/pynchy/git_ops/utils.py` — `run_git()`, `push_local_commits()`

The minimal change: add an optional `env` parameter to `run_git()` and pass it through for operations that hit the remote (`fetch`, `push`, `ls-remote`). Callers that know the `RepoContext` can resolve the token and pass the env.

```python
# utils.py
def run_git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
```

Only remote-facing calls (`fetch origin`, `push origin`, `ls-remote origin`) need the env. Local calls (`status`, `log`, `rev-parse`, `merge`, `rebase`) don't.

### Step 7: Security documentation updates

**File:** `docs/architecture/security.md`

Update the credential table:

| Credential | Admin | Non-Admin | Rationale |
|-----------|-----|---------|-----------|
| `GH_TOKEN` | Broad token | **Repo-scoped token** | Non-admin containers get a fine-grained PAT limited to their `repo_access` repo. |

Update the "Non-LLM credentials" section to explain the scoping model.

### Step 8: Startup validation and token expiry warnings

At startup, after loading config:

1. Warn if a repo has no token — it might be public, but private repos will fail.
2. Check token expiry via the GitHub API and warn when approaching expiration.

**File:** `src/pynchy/git_ops/worktree.py` — in `reconcile_worktrees_at_startup()`

```python
token = get_repo_token(slug)
if token is None:
    logger.warning(
        "No git token for repo — private repos will fail to clone",
        slug=slug,
    )
```

**File:** new function in `src/pynchy/git_ops/repo.py` (or `credentials.py`)

```python
import datetime
import json
import subprocess

# Warn when a token expires within this many days
_EXPIRY_WARNING_DAYS = 30


def check_token_expiry(slug: str, token: str) -> None:
    """Check a fine-grained PAT's expiry via the GitHub API.

    Logs a warning if the token expires within _EXPIRY_WARNING_DAYS.
    Logs an error if the token is already expired.
    Silently succeeds if the API call fails (network issues, classic token, etc.).
    """
    try:
        result = subprocess.run(
            ["gh", "api", "/user", "-H", f"Authorization: token {token}",
             "--jq", "."],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return  # Can't check — might be a classic token or network issue

        # Fine-grained PATs include token_expiry in the response headers.
        # Use the /rate_limit endpoint which returns token metadata.
        result = subprocess.run(
            ["gh", "api", "/rate_limit",
             "-H", f"Authorization: token {token}",
             "-i"],  # include response headers
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return

        # Parse github-authentication-token-expiration header
        for line in result.stdout.splitlines():
            if line.lower().startswith("github-authentication-token-expiration:"):
                expiry_str = line.split(":", 1)[1].strip()
                # Format: "2024-11-30 09:00:00 UTC"
                expiry = datetime.datetime.strptime(
                    expiry_str, "%Y-%m-%d %H:%M:%S %Z"
                ).replace(tzinfo=datetime.timezone.utc)
                now = datetime.datetime.now(datetime.timezone.utc)
                days_left = (expiry - now).days

                if days_left < 0:
                    logger.error(
                        "Repo token has EXPIRED — git operations will fail",
                        slug=slug,
                        expired_on=expiry_str,
                    )
                elif days_left <= _EXPIRY_WARNING_DAYS:
                    logger.warning(
                        "Repo token expiring soon",
                        slug=slug,
                        expires=expiry_str,
                        days_left=days_left,
                    )
                else:
                    logger.debug(
                        "Repo token expiry OK",
                        slug=slug,
                        days_left=days_left,
                    )
                return
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.debug("Could not check token expiry", slug=slug, err=str(exc))
```

Call `check_token_expiry()` during `reconcile_worktrees_at_startup()` for each repo that has an explicit token configured. Only check per-repo tokens (not the broad fallback — that's the user's own `gh` CLI session).

## Files Changed

| File | Change |
|------|--------|
| `src/pynchy/config.py` | Add `token: SecretStr \| None` to `RepoConfig` |
| `src/pynchy/git_ops/repo.py` | `ensure_repo_cloned()` uses token; add `get_repo_token()` helper; add `_sanitize_token()` |
| `src/pynchy/git_ops/utils.py` | Add `env` parameter to `run_git()`; add `git_env_with_token()` |
| `src/pynchy/git_ops/sync.py` | Thread per-repo env through remote git calls |
| `src/pynchy/git_ops/worktree.py` | Thread per-repo env through fetch calls; add startup warning |
| `src/pynchy/container_runner/_credentials.py` | Inject repo-scoped `GH_TOKEN` for non-admin containers with `repo_access` |
| `container/entrypoint.sh` | Configure git credential helper when `GH_TOKEN` is set |
| `docs/architecture/security.md` | Update credential table and scoping documentation |

## Testing

1. **Unit: `get_repo_token()` resolution chain** — test fallback order: per-repo → broad → gh CLI → None.
2. **Unit: `ensure_repo_cloned()` with token** — mock subprocess, verify token is in clone URL, verify remote URL is reset after clone, verify token is sanitized from error logs.
3. **Unit: `_write_env_file()` scoped injection** — admin gets broad token, non-admin with repo_access gets scoped token, non-admin without repo_access gets nothing.
4. **Unit: `check_token_expiry()`** — mock the GitHub API response headers, verify warnings for near-expiry and errors for expired tokens.
5. **Integration: clone a private repo** — requires a test repo with a fine-grained PAT.
6. **Integration: non-admin container `git fetch`** — verify the container can fetch from its designated repo using the injected token.
7. **Security: token not in logs** — grep structured log output for token strings after clone operations.

## Migration

**Backward compatible.** Existing setups with no per-repo tokens continue working:
- Public repos clone without auth (bare URL).
- Admin containers get the broad `gh_token` as before.
- Non-admin containers without `repo_access` get no token as before.
- Host-side sync falls back to ambient `gh` CLI credentials.

**To enable for a private repo:** User creates a fine-grained PAT in GitHub → adds it to config:

```toml
[repos."owner/private-repo"]
token = "github_pat_..."
```

That's it. Next startup, pynchy clones the repo and injects the scoped token into any workspace with `repo_access = "owner/private-repo"`.

## Scope Boundaries

**In scope:** Credential management — config, cloning, host-side auth, container injection, token expiry warnings.

**Out of scope (separate plans):**
- **Workspace git workflow policies** — configurable push behavior (merge-to-main via IPC vs. branch + PR via direct push). See [workspace-git-policies.md](workspace-git-policies.md). Depends on this plan for scoped tokens.
- **Automated token refresh** — GitHub App integration for auto-generated, short-lived tokens. Backlog item for future scope.
- **Multiple repos per workspace** — not needed now.
