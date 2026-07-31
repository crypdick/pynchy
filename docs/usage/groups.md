# Groups

Groups are isolated runtime contexts — each one has its own provider session,
filesystem, and container sandbox. Profiles may also grant access to shared
Obsidian memory.

## Group Management

- Add new groups explicitly via the admin channel
- Groups register in SQLite (via the admin channel or IPC `register_group` command)
- Each group gets a dedicated folder under `groups/`
- Configure additional directory mounts via `containerConfig` (see [Container isolation](../architecture/container-isolation.md))

## Admin Channel Privileges

The admin channel is the admin/control group (typically your WhatsApp self-chat).

| Capability | Admin | Non-Admin |
|------------|-----|---------|
| Sender filter | All channel members accepted | `allowed_users` when configured; otherwise all channel members accepted |
| Schedule tasks for any group | Yes | Own group only |
| View and manage all tasks | Yes | Own group only |
| Configure additional directory mounts | Yes | No |
| Send messages to other chats | Yes | No |
| Edit the personalization repository through the project mount | Yes | No |
| Create and improve canonical personalization skills | Yes | Yes |
| MCP service tools (calendar, etc.) | Auto-approved | Policy-gated |

Non-admin groups can inherit profile `repo` entries, giving them read-write worktree mounts at `/workspace/repos/<owner>/<repo>`. Shared agent instructions are delivered via [prompts](prompts.md) rather than filesystem mounts. The host restricts IPC commands from non-admin groups (see [IPC Authorization](../architecture/security.md#4-ipc-authorization)).
