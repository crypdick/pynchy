# Develop in an isolated deterministic runtime

Use `new-feature` to create feature branches with dedicated worktrees and local Pynchy
services. Each managed feature and the CI runtime lane uses the same deterministic profile:
dedicated server, gateway, and Temporal ports; SQLite, Temporal, and PostgreSQL data; and a
namespaced Docker resource namespace.

## Prerequisites

Install `uv`, then run the repository bootstrapper:

```bash
./scripts/install_new_feature_dependencies.py
```

The bootstrapper verifies Docker and installs `new-feature` and Codex CLIs into `~/.local/bin`.
It installs the pinned Temporal release in the selected bin directory and verifies its SHA-256
archive digest before installation. It does not install or start Docker because that requires
platform-specific system administration. Run with `--check` to diagnose without installing.

CI and standalone deterministic runtime tests only need Docker and Temporal:

```bash
./scripts/install_new_feature_dependencies.py --runtime-only
```

This mode skips the `new-feature` and Codex CLIs. Add its `--bin-dir` to `PATH` when you use a
non-default directory.

## Deterministic profile

Runtime setup generates its own `config.toml`, LiteLLM configuration, and `.env`. It never copies
the control checkout's `.env`, `config.toml`, LiteLLM configuration, provider credentials, or
channel credentials. LiteLLM exposes one `pynchy-deterministic` route to an in-network
OpenAI-compatible sidecar that returns a fixed response. The profile makes no provider calls and
does not download or run a local model.

The generated `.env` contains only runtime-owned values, including an ephemeral gateway key.
The gateway and sidecar share a namespaced Docker network, and Pynchy runs with runtime-owned
HOME/XDG directories so host CLI credentials are not discovered.

## Run runtime integration tests locally

From an isolated, otherwise clean checkout, run the same command as CI:

```bash
uv run python scripts/runtime_harness.py run -- \
  uv run pytest -o addopts='' -n 0 -m runtime
```

Do not use `run` in a worktree whose runtime is already running; stop that runtime first so this
command owns setup and teardown. The command stops its owned processes and containers when it
finishes. On failure, inspect `logs/pynchy-runtime/` and `data/`.

## Create a feature

Run lifecycle commands from the control checkout:

```bash
new-feature create "describe the feature"
```

Setup creates `.worktrees/<slug>`, installs dependencies, initializes `messages.db` and
`memories.db`, starts a dedicated Temporal server, and launches Pynchy with namespaced LiteLLM,
PostgreSQL, and deterministic OpenAI sidecar containers. PostgreSQL uses a namespaced Docker
volume so container ownership cannot prevent worktree removal. Generated configuration, logs,
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
