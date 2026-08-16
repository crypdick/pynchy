# Kubernetes

The manifests in `deploy/k3s` run Pynchy, LiteLLM, PostgreSQL, Temporal, and
Temporal UI in one namespace. They target a single-node K3s installation and
do not replace unrelated Docker Compose or Dockge workloads on the host.

## Storage boundary

Pynchy receives one persistent volume at `/srv/pynchy`. Agent and managed MCP
Pods can mount only subdirectories of that volume. The Kubernetes runtime
rejects host paths outside `PYNCHY_KUBERNETES_SHARED_ROOT`; it does not expose
the host root filesystem or Docker socket.

`deploy/k3s/storage.yaml` uses static local volumes with a `Retain` reclaim
policy. Change its node name and local paths before applying it on another
host. Back up both local volume paths and the namespace secrets.

## Prepare and deploy

1. Populate these directories under the shared volume:

   - `app`: Pynchy checkout, configuration, `data`, and `groups`
   - `vault`: Obsidian vault configured as `learning.obsidian.vault_root`
   - `repos`: repositories configured as `repos.root`
   - `external`: any explicitly configured external files

2. Build `pynchy-host:shadow` from `deploy/k3s/host.Dockerfile`. Build the
   configured agent and private MCP images, then import the local images into
   K3s containerd.
3. Create `pynchy-env` from the migrated Pynchy environment file. Create
   `pynchy-runtime` with `POSTGRES_USER`, `POSTGRES_PASSWORD`, `DATABASE_URL`,
   `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `UI_USERNAME`, and `UI_PASSWORD`.
   Do not commit either Secret.
4. Apply `deploy/k3s` with Kustomize and wait for all workloads to become
   ready.

The host image includes `kubectl`; the Pynchy service account has Pod, Pod log,
and Service permissions only inside the `pynchy` namespace. K3s hosts with a
default-deny host firewall must allow the K3s Pod CIDR to reach node-local
cluster services.

## Shadow validation and cutover

The checked-in manifest is deliberately safe for shadow validation:

- channel plugins are disabled;
- `scheduler.reconcile_schedules` is `false`, so the Temporal worker can run
  test workflows without creating schedules or delayed workflows;
- `scheduler.auto_deploy` is `false`, because Kubernetes releases replace
  images instead of mutating a running source checkout;
- the runtime harness is enabled for an injected end-to-end message.

Before cutover, prove service health, an injected agent response, required MCP
tools, LiteLLM persistence, Temporal persistence, restart recovery, backup and
restore, and rollback. Then stop the old service, take the final SQLite-safe
copy, disable the runtime harness, enable the real channel plugins, and set
`scheduler.reconcile_schedules = true` on the new deployment.

Do not restore a single-host Temporal SQLite database into PostgreSQL. Archive
it for audit and rollback. Pynchy reconstructs current recurring schedules and
delayed work from its configuration and SQLite state when reconciliation is
enabled. Compare the reconstructed Temporal inventory with the old cluster
before declaring cutover complete.
