# Migration

Move an existing Pynchy installation to a new checkout or host without copying
runtime state that can disrupt the new service.

## Copy persistent configuration

Stop the old service before copying these files into the same relative paths in
the new checkout:

- `data/`
- `config.toml`
- `litellm_config.yaml`
- `.env` when it contains gateway, channel, or provider secrets

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

Pynchy prunes migration safety copies from `data/migration-backups/` after a
successful deploy restart, retaining the newest three directories. Inspect or
run cleanup explicitly with:

```bash
uv run pynchy prune-migration-backups
uv run pynchy prune-migration-backups --keep 2 --apply
```

The command considers only direct child directories and ignores files and
symlinks.
