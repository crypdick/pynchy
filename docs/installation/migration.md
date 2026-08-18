# Migration

Move an existing Pynchy installation to a new checkout or host without copying
runtime state that can disrupt the new service.

## Copy persistent configuration

Stop the old service. Clone the private personalization repository into the new
checkout first:

```bash
git clone git@github.com:YOUR-ACCOUNT/pynchy-personalization.git \
  data/personalization
```

Then copy runtime data without replacing that nested repository. Copy the root
`.env` only for a local-development deployment that still uses it:

```bash
rsync -a --exclude personalization OLD_PYNCHY/data/ NEW_PYNCHY/data/
cp OLD_PYNCHY/.env NEW_PYNCHY/.env
```

For production, configure the new service to materialize the declared names
from Proton Pass instead of copying plaintext values.

For an installation that still uses root `config.toml` and
`litellm_config.yaml`, initialize a private personalization repository, move
their contents to `pynchy.toml` and `litellm.yaml`, and remove
`gateway.litellm_config` from the TOML. Pynchy no longer reads the root files.
Move durable `[jobs.<name>]` declarations into
`automations/<name>/config.toml` documents when convenient; the validator still
accepts non-colliding `[jobs]` declarations during migration.

Convert legacy credential delivery before starting the new service:

- Move MCP `env_forward` names to the owning tool's `required_env` or
  `optional_env`.
- Replace `workspaces.<name>.proton_pass_env_file` with tool-owned
  requirements.
- Declare agent-side CLI credentials on a `type = "workspace"` tool instead of
  relying on implicit GitHub injection.
- Remove generated `data/env/` directories after stopping the old service.
- Materialize production values into the new Pynchy host process through
  Proton Pass.

See [Tool access and secrets](../usage/tool-access.md) for the target
configuration.

Validate before the first start:

```bash
uv run pynchy validate-personalization data/personalization
```

Do not copy `data/deploy_continuation.json`; it can trigger a rollback to an old
commit on the new host.

If `data/neonize.db` moves successfully, skip WhatsApp QR authentication unless
the linked-device session expired. Do not blindly copy LiteLLM's PostgreSQL data
between Linux Docker and macOS Apple Container deployments; let the gateway
recreate its database unless you need its internal history.

## Review runtime-only state

`data/` can contain host-specific worktrees, repositories, and message history.
If startup stalls on `git fetch` or a repo-backed workspace because the new host
cannot reach GitHub yet, move `data/worktrees/` and `data/repos/` aside or
temporarily remove the affected `repo` profile entries. Prioritize one healthy
service over preserving every historical row.
