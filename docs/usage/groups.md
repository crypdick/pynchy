# Groups

This page covers how to manage groups and understand the god channel's admin privileges. Groups provide isolated contexts — each group has its own memory, filesystem, and container sandbox.

## Group Management

- Add new groups explicitly via the god channel
- Groups register in SQLite (via the god channel or IPC `register_group` command)
- Each group gets a dedicated folder under `groups/`
- Configure additional directory mounts via `containerConfig` (see [Container isolation](../architecture/container-isolation.md))

## God Channel Privileges

The god channel serves as the admin/control group (typically your WhatsApp self-chat).

| Capability | God | Non-God |
|------------|-----|---------|
| Write to global memory (`groups/global/CLAUDE.md`) | Yes | No |
| Schedule tasks for any group | Yes | Own group only |
| View and manage all tasks | Yes | Own group only |
| Configure additional directory mounts | Yes | No |
| Send messages to other chats | Yes | No |
| Edit `config.toml` (mounted read-write) | Yes | No |
| MCP service tools (calendar, etc.) | Auto-approved | Policy-gated |

Non-god groups can have `project_access` (configured in `workspace.yaml`), giving them a read-write worktree mount at `/workspace/project` instead of the readonly `groups/global/` mount. The host restricts IPC commands from non-god groups (see [IPC Authorization](../architecture/security.md#4-ipc-authorization)).
