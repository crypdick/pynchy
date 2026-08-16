# Kubernetes

The manifests in `deploy/k3s` run Pynchy, LiteLLM, Pocket TTS, PostgreSQL,
Temporal, and Temporal UI in one namespace. They target a single-node K3s
installation and do not replace unrelated Docker Compose or Dockge workloads
on the host.

## Storage boundary

Pynchy receives one persistent volume at `/srv/pynchy`. Agent and managed MCP
Pods can mount only subdirectories of that volume. The Kubernetes runtime
rejects host paths outside `PYNCHY_KUBERNETES_SHARED_ROOT`; it does not expose
the host root filesystem or Docker socket.

`deploy/k3s/storage.yaml` uses static local volumes with a `Retain` reclaim
policy. Change its node name and local paths before applying it on another
host. Back up all three local volume paths and the namespace secrets. Pocket
TTS uses its own volume only for downloaded model caches.

## Prepare and deploy

1. Populate these directories under the shared volume:

   - `app`: Pynchy checkout, configuration, `data`, and `groups`
   - `vault`: Obsidian vault configured as `learning.obsidian.vault_root`
   - `repos`: repositories configured as `repos.root`
   - `external`: any explicitly configured external files

2. Build `pynchy-host:shadow` from `deploy/k3s/host.Dockerfile` and
   `pynchy-pocket-tts:shadow` from `deploy/k3s/pocket-tts.Dockerfile`. Build
   the configured agent and private MCP images, then import the local images
   into K3s containerd.
3. Create `pynchy-env` from the migrated Pynchy environment file. Create
   `pynchy-runtime` with `POSTGRES_USER`, `POSTGRES_PASSWORD`, `DATABASE_URL`,
   `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `UI_USERNAME`, and `UI_PASSWORD`.
   Do not commit either Secret.
4. Apply `deploy/k3s` with Kustomize and wait for all workloads to become
   ready.

The host image includes `kubectl`, Codex, and GitHub CLI for direct-host admin
workspaces. The Pynchy service account has Pod, Pod log, and Service permissions
only inside the `pynchy` namespace. K3s hosts with a default-deny host firewall
must allow the K3s Pod CIDR to reach node-local cluster services.

## Android USB

Do not make the Pynchy Pod privileged or mount `/dev/bus/usb` into it. Install
`adb` and the distribution's Android udev rules on the node, create the
unprivileged `pynchy-adb` system user in the `plugdev` group, and install
`deploy/k3s/pynchy-adb.service`. The service exposes ADB through one Unix socket,
which the manifest mounts read-only into the Pynchy container.

Set this variable in the Android MCP tool environment:

```toml
ADB_SERVER_SOCKET = "localfilesystem:/run/pynchy-adb/adb.sock"
```

Authorize the node's ADB key on the phone before running the Android MCP wet
test.

## Shadow validation and cutover

The checked-in manifest runs production channels and schedule reconciliation.
For a shadow deployment, copy `deploy/k3s/pynchy.yaml` and make these temporary
changes before applying it:

- set `PLUGINS__DISCORD__ENABLED` and `PLUGINS__LINEAR__ENABLED` to `false`;
- set `SCHEDULER__RECONCILE_SCHEDULES` to `false` so the Temporal worker can run
  test workflows without creating schedules or delayed workflows;
- add `PYNCHY_RUNTIME_HARNESS=1` for injected end-to-end messages.

Keep `SCHEDULER__AUTO_DEPLOY=false`. Kubernetes releases replace images instead
of mutating a running source checkout.

Before cutover, prove service health, an injected agent response, required MCP
tools, LiteLLM persistence, Temporal persistence, restart recovery, backup and
restore, and rollback. Then stop the old service, take the final SQLite-safe
copy, apply the production manifest, and verify the reconstructed Temporal
inventory before routing messages to the new deployment.

Do not restore a single-host Temporal SQLite database into PostgreSQL. Archive
it for audit and rollback. Pynchy reconstructs current recurring schedules and
delayed work from its configuration and SQLite state when reconciliation is
enabled. Compare the reconstructed Temporal inventory with the old cluster
before declaring cutover complete.
