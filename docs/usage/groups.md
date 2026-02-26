# Groups

This page covers how to manage groups and understand the admin channel's privileges. Groups provide isolated contexts — each group has its own memory, filesystem, and container sandbox.

## Group Management

- Add new groups explicitly via the admin channel
- Groups register in SQLite (via the admin channel or IPC `register_group` command)
- Each group gets a dedicated folder under `groups/`
- Configure additional directory mounts via `containerConfig` (see [Container isolation](../architecture/container-isolation.md))

## Admin Channel Privileges

The admin channel serves as the admin/control group (typically your WhatsApp self-chat).

| Capability | Admin | Non-Admin |
|------------|-----|---------|
| Sender filter | All channel members accepted | `allowed_users` (default: owner only) |
| Schedule tasks for any group | Yes | Own group only |
| View and manage all tasks | Yes | Own group only |
| Configure additional directory mounts | Yes | No |
| Send messages to other chats | Yes | No |
| Edit `config.toml` (mounted read-write) | Yes | No |
| MCP service tools (calendar, etc.) | Auto-approved | Policy-gated |

Non-admin groups can have `repo_access` (configured in `config.toml`), giving them a read-write worktree mount at `/workspace/project`. Shared agent instructions are delivered via [directives](directives.md) rather than filesystem mounts. The host restricts IPC commands from non-admin groups (see [IPC Authorization](../architecture/security.md#4-ipc-authorization)).
