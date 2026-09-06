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
node name, and local paths. Keep the `Retain` reclaim policy. Back up the shared,
PostgreSQL, and desktop-profile paths plus the namespace secrets. Pocket TTS
uses its own volume only for downloaded model caches.

`deploy/k3s/bootstrap` contains namespace-scoped service accounts and RBAC.
`deploy/k3s/application` contains the workloads and network policy that the
release monitor reconciles. The root Kustomization includes both for initial
installation.

## Prepare and deploy

1. Populate these directories under the shared volume:

   - `app`: Pynchy checkout, configuration, `data`, and `groups`
   - `vault`: Obsidian vault configured as `learning.obsidian.vault_root`
   - `repos`: repositories configured as `repos.root`
   - `external`: any explicitly configured external files

   Keep the shared tree owned by UID and GID `3000`, including restored
   `.runtime` volume directories. The Pynchy host runs as that identity and
   must be able to prepare writable mounts for managed agent and MCP Pods.

2. Build `pynchy-pocket-tts:shadow` from
   `deploy/k3s/pocket-tts.Dockerfile` plus any private MCP images, then import
   those local images into K3s containerd. For Pynchy host and agent images,
   use one successful full-SHA release from GitHub Container Registry in the
   deployment overlay. The isolated desktop and release monitor use the same
   host SHA. Do not build separate local Pynchy host or agent images for K3s.
3. Create `pynchy-env` from the migrated Pynchy environment file. Create
   `pynchy-runtime` with `POSTGRES_USER`, `POSTGRES_PASSWORD`, `DATABASE_URL`,
   `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `UI_USERNAME`, and `UI_PASSWORD`.
   Do not commit either Secret.
4. Add these release values to `pynchy-env`:

   ```dotenv
   PYNCHY_RELEASE_REPOSITORY=OWNER/REPOSITORY
   PYNCHY_RELEASE_HOST_IMAGE=ghcr.io/OWNER/pynchy-host
   PYNCHY_RELEASE_AGENT_IMAGE=ghcr.io/OWNER/pynchy-agent
   PYNCHY_RELEASE_PATCH=/srv/pynchy/app/PATH/TO/DEPLOYMENT-PATCH.yaml
   ```

   `GITHUB_TOKEN` must be able to read repository metadata. Create the
   `pynchy-ghcr` image pull Secret separately with credentials that can read
   both packages. `PYNCHY_RELEASE_PATCH` is optional; use it for
   deployment-specific changes such as a vault volume. The patch must stay
   inside the scoped shared volume. Keep repository names, owners, paths, and
   credentials in the deployment-specific overlay or Secret, not in the public
   base.
5. Install the desktop browser's AppArmor profile on every node that can run
   `pynchy-desktop`:

   ```bash
   sudo install -m 0644 deploy/k3s/pynchy-chromium.apparmor \
     /etc/apparmor.d/pynchy-chromium
   sudo apparmor_parser -r /etc/apparmor.d/pynchy-chromium
   ```

   The desktop container has no Linux capabilities and cannot gain privileges.
   Its container seccomp profile is unconfined only so Chromium can create a
   user namespace; Chromium then applies its own seccomp filters to renderer
   processes.
6. Apply the deployment-specific Kustomize overlay and wait for all workloads
   to become ready.

## Release from main

The `Test` GitHub Actions workflow publishes immutable host and agent images
tagged with the full commit SHA only after the Python and deterministic runtime
jobs pass. The `pynchy-release-monitor` CronJob queries the latest successful
push-to-main workflow every two minutes. That successful workflow is the
release pointer: both images have passed tests and publication before the
monitor selects its SHA.

The monitor starts isolated host and agent preflight Pods. If Kubernetes still
reports either image as pending, it deletes the probes and exits successfully;
the next scheduled run retries without creating a failed release Job or
changing production. Once both probes pass, the monitor fast-forwards the
persistent checkout to the selected SHA. It refuses tracked changes or a
non-fast-forward update.

The monitor renders `deploy/k3s/application` with exact host and agent images,
the release SHA, and the optional deployment patch, then applies it. This
updates Pynchy, the desktop, PostgreSQL, Temporal, services, network policy,
and the monitor itself from the same source revision.

The monitor waits for PostgreSQL, Temporal, Pynchy, and desktop rollouts, then
requires the desktop helper, Pynchy service status, release accounting,
LiteLLM, and Temporal to report healthy. A failed rollout or health check
reapplies the previously rendered application manifests and restores the
previous checkout SHA. Pynchy and desktop use `Recreate`, so rollback protects
availability but does not guarantee zero downtime.

## Temporal namespace retention

The Temporal Deployment owns retention for the `default` namespace. Its
auto-setup container creates new namespaces with a `192h` retention period,
and its retention reconciler verifies and restores that value every five
minutes. This is eight days, exceeding the seven-day audit-history minimum.
Previously expired histories cannot be recovered.

Check release state with:

```bash
kubectl -n pynchy get cronjob pynchy-release-monitor
kubectl -n pynchy get jobs --sort-by=.metadata.creationTimestamp
kubectl -n pynchy logs job/JOB_NAME
kubectl -n pynchy get deployment pynchy \
  -o 'jsonpath={.spec.template.metadata.annotations.pynchy\.dev/release-sha}'
kubectl -n pynchy get deployment pynchy-desktop \
  -o 'jsonpath={.spec.template.metadata.annotations.pynchy\.dev/release-sha}'
kubectl -n pynchy get cronjob pynchy-release-monitor \
  -o 'jsonpath={.metadata.annotations.pynchy\.dev/release-sha}'
kubectl -n pynchy exec deploy/pynchy -c pynchy -- \
  /opt/pynchy/.venv/bin/pynchy status
kubectl -n pynchy exec deploy/pynchy-temporal -c retention-reconciler -- \
  temporal operator namespace describe --namespace default --output json
```

Application manifest changes deploy automatically with their successful image
release. Namespace, service-account, RBAC, Secret, persistent-volume, Pocket
TTS, and private MCP image changes remain explicit operator work. This keeps
the monitor from granting itself permissions or changing storage authority.

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

## Configuration and read-only diagnostics

The Pynchy container works in `/srv/pynchy/app`. Its settings layers are
`data/defaults/pynchy.toml`, `data/personalization/pynchy.toml`, then environment
overrides, including those supplied by the `pynchy-env` Secret. There is no
`config.toml` beside the database. See the
[personalization contract](../usage/personalization.md#directory-contract) for
configuration precedence and related files. Inspect paths or selected non-secret
fields; never dump the Secret, environment, or full configuration into diagnostics.

From an operator checkout with private `[ops]` settings, prefer `uv run pynchy ops
events` and `uv run pynchy ops messages`. Both open the database read-only and wait
up to five seconds for a writer lock. For a custom bounded query:

```bash
kubectl -n pynchy exec deploy/pynchy -c pynchy -- \
  sqlite3 -readonly -cmd '.timeout 5000' /srv/pynchy/app/data/messages.db \
  'SELECT timestamp, event_type FROM events ORDER BY timestamp DESC LIMIT 20;'
```

Persistent lock failures need diagnosis; do not delete database sidecars or restart
the service solely to make a diagnostic query succeed.

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
For a shadow deployment, copy `deploy/k3s/application/pynchy.yaml` and make
these temporary changes before applying it:

- set `PLUGINS__DISCORD__ENABLED` and `PLUGINS__LINEAR__ENABLED` to `false`;
- set `SCHEDULER__RECONCILE_SCHEDULES` to `false` so the Temporal worker can run
  test workflows without creating schedules or delayed workflows;
- add `PYNCHY_RUNTIME_HARNESS=1` for injected end-to-end messages.

Keep `SCHEDULER__AUTO_DEPLOY=false`. The release monitor replaces Kubernetes
images instead of asking Pynchy to restart itself from a changed checkout.

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
