# Groups

## Group Management

- New groups are added explicitly via the god channel
- Groups are registered in SQLite (via the god channel or IPC `register_group` command)
- Each group gets a dedicated folder under `groups/`
- Groups can have additional directories mounted via `containerConfig` (see [Container isolation](container-isolation.md))

## God Channel Privileges

The god channel is the admin/control group (typically your WhatsApp self-chat).

| Capability | God | Non-God |
|------------|-----|---------|
| Write to global memory (`groups/global/CLAUDE.md`) | Yes | No |
| Schedule tasks for any group | Yes | Own group only |
| View and manage all tasks | Yes | Own group only |
| Configure additional directory mounts | Yes | No |
| Send messages to other chats | Yes | No |

Non-god groups can have `project_access` (configured in `workspace.yaml`), which gives them a read-write worktree mount at `/workspace/project` instead of the readonly `groups/global/` mount. IPC commands from non-god groups are restricted by the host (see [IPC Authorization](../security.md#4-ipc-authorization)).
