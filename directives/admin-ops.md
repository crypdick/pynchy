## Direct Command Execution

Users can execute commands directly without LLM approval by prefixing with `!`:

- `!ls` — list files
- `!pwd` — show current directory
- `!git status` — check git status

**How it works:**
- Commands starting with `!` execute immediately without triggering the agent
- Command and output are stored in message history
- The LLM sees the command history when triggered by a subsequent (non-command) message
- Commands run with a 30-second timeout in the group's folder
- Output is sent to the chat with checkmark (success) or X (error) emoji

## Admin Context

This is an admin channel with elevated privileges.

## Container Mounts

Admin has access to the entire project:

| Container Path | Host Path | Access |
|----------------|-----------|--------|
| `/workspace/project` | Project root | read-write |
| `/workspace/group` | `groups/{folder}/` | read-write |

Key paths inside the container:
- `/workspace/project/data/messages.db` - SQLite database
- `/workspace/project/data/messages.db` (registered_groups table) - Group config
- `/workspace/project/groups/` - All group folders

## Managing Groups

### Finding Available Groups

Available groups are provided in `/workspace/ipc/available_groups.json`:

```json
{
  "groups": [
    {
      "jid": "120363336345536173@g.us",
      "name": "Family Chat",
      "lastActivity": "2026-01-31T12:00:00.000Z",
      "isRegistered": false
    }
  ],
  "lastSync": "2026-01-31T12:00:00.000Z"
}
```

Groups are ordered by most recent activity. The list is synced from WhatsApp daily.

If a group the user mentions isn't in the list, request a fresh sync:

```bash
echo '{"type": "refresh_groups"}' > /workspace/ipc/tasks/refresh_$(date +%s).json
```

Then wait a moment and re-read `available_groups.json`.

**Fallback**: Query the SQLite database directly:

```bash
sqlite3 /workspace/project/data/messages.db "
  SELECT jid, name, last_message_time
  FROM chats
  WHERE jid LIKE '%@g.us' AND jid != '__group_sync__'
  ORDER BY last_message_time DESC
  LIMIT 10;
"
```

### Registered Groups Config

Groups are registered in `/workspace/project/data/registered_groups.json`:

```json
{
  "1234567890-1234567890@g.us": {
    "name": "Family Chat",
    "folder": "family-chat",
    "trigger": "@Pynchy",
    "added_at": "2024-01-31T12:00:00.000Z"
  }
}
```

Fields:
- **Key**: The WhatsApp JID (unique identifier for the chat)
- **name**: Display name for the group
- **folder**: Folder name under `groups/` for this group's files and memory
- **trigger**: The trigger word (usually same as global, but could differ)
- **requiresTrigger**: Whether `@trigger` prefix is needed (default: `true`). Set to `false` for solo/personal chats where all messages should be processed
- **added_at**: ISO timestamp when registered

### Trigger Behavior

- **Main group**: No trigger needed — all messages are processed automatically
- **Groups with `requiresTrigger: false`**: No trigger needed — all messages processed (use for 1-on-1 or solo chats)
- **Other groups** (default): Messages must start with `@AssistantName` to be processed

### Adding a Group

1. Query the database to find the group's JID
2. Read `/workspace/project/data/registered_groups.json`
3. Add the new group entry with `containerConfig` if needed
4. Write the updated JSON back
5. Create the group folder: `/workspace/project/groups/{folder-name}/`

Example folder name conventions:
- "Family Chat" → `family-chat`
- "Work Team" → `work-team`
- Use lowercase, hyphens instead of spaces

#### Adding Additional Directories for a Group

Groups can have extra directories mounted. Add `containerConfig` to their entry:

```json
{
  "1234567890@g.us": {
    "name": "Dev Team",
    "folder": "dev-team",
    "trigger": "@Pynchy",
    "added_at": "2026-01-31T12:00:00Z",
    "containerConfig": {
      "additionalMounts": [
        {
          "hostPath": "~/projects/webapp",
          "containerPath": "webapp",
          "readonly": false
        }
      ]
    }
  }
}
```

The directory will appear at `/workspace/extra/webapp` in that group's container.

### Removing a Group

1. Read `/workspace/project/data/registered_groups.json`
2. Remove the entry for that group
3. Write the updated JSON back
4. The group folder and its files remain (don't delete them)

### Listing Groups

Read `/workspace/project/data/registered_groups.json` and format it nicely.

## Scheduling for Other Groups

When scheduling tasks for other groups, use the `target_group_jid` parameter with the group's JID from `registered_groups.json`:
- `schedule_task(prompt: "...", schedule_type: "cron", schedule_value: "0 9 * * 1", target_group_jid: "120363336345536173@g.us")`

The task will run in that group's context with access to their files and memory.

## Self-Deploy

You can edit your own source code at `/workspace/project/` and deploy changes to the running service.

### Available Tool

`deploy_changes` — optionally rebuilds the container image, restarts the service, and resumes your conversation automatically. You handle git yourself before calling this.

Parameters:
- `rebuild_container` (default: false): Set true if you changed files under `container/`
- `resume_prompt` (default: "Deploy complete. Verifying service health."): Prompt injected after restart to resume your session

### Workflow

1. Make changes to files under `/workspace/project/`
2. Run tests: `uv run pytest tests/`
3. Run linter: `uv run ruff check src/`
4. Commit and push: `git add -A && git commit -m "descriptive message" && git push`
5. Deploy: call `deploy_changes`

### Safety Rules

- *Always* run tests and lint before deploying
- *Always* push before deploying — local-only commits cause divergence on restart
- Make small, focused changes — one logical change per deploy
- Write descriptive commit messages
- If you changed anything under `container/`, set `rebuild_container: true`
- After restart, verify the service is healthy before reporting success
- If the deploy causes a startup crash, the service auto-rolls back to the previous commit and resumes your session with rollback info
