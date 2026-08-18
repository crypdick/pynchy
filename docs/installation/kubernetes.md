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

The base Kustomization omits storage because local volume paths and node names
belong to each deployment. Copy `deploy/k3s/storage.example.yaml` into a
deployment-specific Kustomize overlay, then replace its sample volume names,
node name, and local paths. Keep the `Retain` reclaim policy. Back up all three
local volume paths and the namespace secrets. Pocket TTS uses its own volume
only for downloaded model caches.

## Prepare and deploy

1. Populate these directories under the shared volume:

   - `app`: Pynchy checkout, configuration, `data`, and `groups`
   - `vault`: Obsidian vault configured as `learning.obsidian.vault_root`
   - `repos`: repositories configured as `repos.root`
   - `external`: any explicitly configured external files

   Keep the shared tree owned by UID and GID `3000`, including restored
   `.runtime` volume directories. The Pynchy host runs as that identity and
   must be able to prepare writable mounts for managed agent and MCP Pods.

2. Build `pynchy-host:shadow` from `deploy/k3s/host.Dockerfile` and
   `pynchy-pocket-tts:shadow` from `deploy/k3s/pocket-tts.Dockerfile`. Build
   the configured agent and private MCP images, then import the local images
   into K3s containerd.
3. Create `pynchy-env` from the migrated Pynchy environment file. Create
   `pynchy-runtime` with `POSTGRES_USER`, `POSTGRES_PASSWORD`, `DATABASE_URL`,
   `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `UI_USERNAME`, and `UI_PASSWORD`.
   Do not commit either Secret.
4. Apply the deployment-specific Kustomize overlay and wait for all workloads
   to become ready.

### Mount a synchronized vault

Keep concrete vault paths, node names, and claims in the deployment-specific
Kustomize overlay. Mount its claim at `/srv/pynchy/vault` in the Pynchy
container and set `PYNCHY_KUBERNETES_VAULT_PVC` to the claim name. Agent Pods
then route vault and automation-memory mounts through that claim while other
mounts remain on the shared claim.

Mount the synchronization service's live vault into the claim's local path
before K3s starts. Make it writable by UID and GID `3000`, and set its root
directory's group to `3000`. The runtime leaves synchronization-owned vault
permissions unchanged.

## Back up runtime state

Install `deploy/k3s/backup.sh` on the node and schedule it with
`deploy/k3s/pynchy-k3s-backup.cron` before the host backup system. It creates
SQLite-safe copies of `messages.db` and `neonize.db` plus native PostgreSQL
dumps of the LiteLLM, Temporal, and Temporal visibility databases. The script
keeps 14 local generations under `PYNCHY_K3S_STORAGE_ROOT`. Set that variable
to the directory containing the `shared`, `postgres`, and `backups`
subdirectories.

Exclude the live `pynchy-k3s/postgres` directory and live SQLite database files
from file-level backup plans. Back up the generated `pynchy-k3s/backups`
directory instead. ZFS snapshots remain useful for short rollback windows, but
they do not replace PostgreSQL dumps or off-host backups.

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
