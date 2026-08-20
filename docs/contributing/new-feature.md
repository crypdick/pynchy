# Develop in an isolated deterministic runtime

Use `new-feature` to create feature branches with dedicated worktrees. Start a local Pynchy
runtime only when interactive runtime diagnosis needs one. Each runtime and the CI lane uses the same deterministic profile:
dedicated server, gateway, and Temporal ports; SQLite, Temporal, and PostgreSQL data; and a
namespaced Docker resource namespace.

## Prerequisites

Install `uv`, then run the repository bootstrapper:

```bash
./scripts/install_new_feature_dependencies.py
```

The bootstrapper verifies Docker and installs the current `new-feature` release and pinned Codex
CLI into `~/.local/bin`. It accepts any functioning installed `new-feature` release so host-level
updaters can keep the tool current without fighting repository bootstrap policy. It installs the
pinned Temporal release in the selected bin directory and verifies its SHA-256 archive digest
before installation. It does not install or start Docker because that requires platform-specific
system administration. Run with `--check` to diagnose without installing.

CI and standalone deterministic runtime tests only need Docker and Temporal:

```bash
./scripts/install_new_feature_dependencies.py --runtime-only
```

This mode skips the `new-feature` and Codex CLIs. Add its `--bin-dir` to `PATH` when you use a
non-default directory.

## Deterministic profile

Runtime setup generates its own `data/personalization/` tree and `.env`. It
never copies the control checkout's `.env`, personalization tree, provider credentials, or
channel credentials. LiteLLM exposes one `pynchy-deterministic` route to an in-network
OpenAI-compatible sidecar that returns a fixed response. The profile makes no provider calls and
does not download or run a local model.

The generated `.env` contains only runtime-owned values, including an ephemeral gateway key.
The gateway and sidecar share a namespaced Docker network. Runtime-owned HOME/XDG directories
and the credential policy keep ambient provider, channel, and GitHub credentials out of the test
agent.

The profile builds a locked, minimal agent-runner image from pinned base images and the nested
`agent_runner/uv.lock`. This image runs the real OpenAI agent core and persistent file-IPC loop
without the mutable CLI and plugin installation in the production agent image. The first setup
downloads the pinned image and locked Python artifacts when the local cache lacks them.
Its tag combines the runtime namespace and source digest; harness teardown removes that exact
test image after its owned containers have stopped.

Interactive runtime tests enter through a harness-only loopback route enabled by
`PYNCHY_RUNTIME_HARNESS=1`. The route calls the same inbound orchestration boundary as a channel;
it never exists in a normal Pynchy process. Tests inspect the harness-owned SQLite database for
durable results instead of exposing production chat-history endpoints.

## Run runtime integration tests locally

From an isolated, otherwise clean checkout, run the same command as CI:

```bash
uv run python scripts/runtime_harness.py run -- \
  uv run pytest -o addopts='' -n 0 -m runtime
```

Do not use `run` in a worktree whose runtime is already running; stop that runtime first so this
command owns setup and teardown. The command stops its owned processes and containers when it
finishes. On failure, inspect `logs/pynchy-runtime/` and `data/`.

For interactive diagnosis, create the runtime first, then use `exec`. `exec` keeps the sandbox
running for diagnosis. It rejects diagnostic state left by a failed `run`, because that command
has already stopped its live resources; inspect its logs and data, then run `stop` before a fresh
setup:

```bash
uv run python scripts/runtime_harness.py setup
uv run python scripts/runtime_harness.py exec -- \
  uv run pytest -o addopts='' -n 0 -m runtime
```

## Create a feature

Pynchy's shared configuration launches the built-in Codex agent when `--agent` is omitted.
Individual agent preferences can override that in the ignored `.new-feature.local.toml` sidecar.
The shared `push = false` policy is deliberate: pushing `main` deploys Pynchy, so deployment stays
an explicit operator action.

Run lifecycle commands from the control checkout:

```bash
new-feature create "describe the feature"
```

Setup creates `.worktrees/<slug>` and installs dependencies. It does not start a runtime. Start
one only for interactive diagnosis:

```bash
uv run python scripts/runtime_harness.py setup
```

The harness initializes `messages.db`, starts a dedicated Temporal server, and
launches Pynchy with namespaced LiteLLM, PostgreSQL, and deterministic OpenAI sidecar containers.
PostgreSQL uses a namespaced Docker volume so container ownership cannot prevent worktree removal.
Generated configuration, logs, process state, and databases remain ignored inside the feature
worktree.

If runtime setup fails, the harness copies its logs to
`.new-feature/diagnostics/runtime-setup-failures/<slug>/` in the control checkout before
`new-feature` removes the partial worktree. The exception reports the exact archive path. The
harness retains the five newest failure archives for each feature slug; successful setup and
teardown do not create archives.

When an agent already runs in the control checkout, prevent a nested agent:

```bash
new-feature create "describe the feature" --no-agent
new-feature list
```

Read `.new-feature/manifest.toml` when the existing shell needs the allocated values. The
generated `.env` and personalization files let commands run normally from the feature worktree.

Inspect or restart the sandbox from its worktree:

```bash
uv run python scripts/runtime_harness.py status
uv run python scripts/runtime_harness.py restart
```

## Publish a review pull request

After committing the feature work, an authorized agent runtime can call the
`publish_managed_feature` lifecycle tool with the feature slug:

```text
publish_managed_feature(feature_slug="<slug>")
```

The tool opens or updates a review pull request only. It never merges the
branch or deploys Pynchy. Commit all feature changes first: the host rejects
uncommitted files and publishes only the validated committed HEAD. The host
derives the repository, worktree, branch, and target branch from the active
managed-feature manifest rather than from agent input. If the target branch
advances, call `rebase_managed_feature(feature_slug="<slug>")` before
publishing. The host verifies the remote default branch and rebases only the
manifest-bound feature. If it reports a conflict, resolve it with `git rebase
--continue`, `--abort`, or `--skip`. See the [IPC
architecture](../architecture/ipc.md#managed-feature-pr-publication) for the
request contract and the [security model](../architecture/security.md#5c-host-mutating-operations-cop-gate)
for its publication checks.

## Merge and remove a feature

Commit the feature work, return to the control checkout, then run:

```bash
new-feature merge <slug>
new-feature teardown <slug>
```

`new-feature merge` performs local integration; it does not publish a review pull request. It
stops the development sandbox, starts a fresh deterministic runtime from the current worktree,
and runs the runtime suite. Its harness always stops live runtime resources before prek hooks
run. It then performs a no-commit merge into `main` and runs both Pynchy and agent-runner tests
against the integrated tree. A failed check aborts the merge; runtime logs and data remain
available for diagnosis. Merge does not push `main`, so deployment remains an explicit operator
action.

Teardown stops any remaining processes, removes only the feature's namespaced LiteLLM and
PostgreSQL resources, and then removes the worktree, branch, manifest entry, and local databases.
