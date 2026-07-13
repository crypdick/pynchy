# Develop in an isolated feature runtime

Use `new-feature` to create feature branches with dedicated worktrees and local Pynchy
services. Each managed feature receives its own server, gateway, and Temporal ports; SQLite,
Temporal, and PostgreSQL data; and Docker resource namespace.

## Prerequisites

Install `uv`, then run the repository bootstrapper:

```bash
./scripts/install_new_feature_dependencies.py
```

The bootstrapper verifies Docker and installs missing user-local Temporal, `new-feature`, and
Codex CLIs into `~/.local/bin`. It downloads a pinned Temporal release and verifies its SHA-256
digest before installation. It does not install or start Docker because that requires
platform-specific system administration. Run with `--check` to diagnose without installing.

Export the provider variables referenced by `litellm_config.yaml` before creating a feature.
Setup fails when no usable model route remains after filtering, rather than starting a gateway
that cannot serve requests.

Keep a working local `litellm_config.yaml` in the control checkout. Sandbox setup copies that
routing configuration and only the environment variables it references. It does not copy
channel credentials or the control checkout's `config.toml`.

## Create a feature

Run lifecycle commands from the control checkout:

```bash
new-feature create "describe the feature"
```

Setup creates `.worktrees/<slug>`, installs dependencies, initializes `messages.db` and
`memories.db`, starts a dedicated Temporal server, and launches Pynchy with namespaced
LiteLLM and PostgreSQL containers. PostgreSQL uses a namespaced Docker volume so container
ownership cannot prevent worktree removal. Generated configuration, credentials, logs,
process state, and databases remain ignored inside the feature worktree.

When an agent already runs in the control checkout, prevent a nested agent:

```bash
new-feature create "describe the feature" --no-agent
new-feature list
```

Read `.new-feature/manifest.toml` when the existing shell needs the allocated values. The
generated `.env` and `config.toml` let commands run normally from the feature worktree.

Inspect or restart the sandbox from its worktree:

```bash
uv run python scripts/new_feature_sandbox.py status
uv run python scripts/new_feature_sandbox.py restart
```

## Merge and remove a feature

Commit the feature work, return to the control checkout, then run:

```bash
new-feature merge <slug>
new-feature teardown <slug>
```

Merge stops the sandbox before running all pre-commit hooks. It then performs a no-commit
merge into `main` and runs pytest against the integrated tree. A failed check aborts the merge;
restart the sandbox from the worktree when more runtime debugging is necessary. Merge does not
push `main`, so deployment remains an explicit operator action.

Teardown stops any remaining processes, removes only the feature's namespaced LiteLLM and
PostgreSQL resources, and then removes the worktree, branch, manifest entry, and local databases.
