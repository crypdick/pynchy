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

### How You Got Here

The pynchy service runs on the host (`mac-mini`). The host maintains a clone of the pynchy repo. When an admin session starts, the service launches a container with:

1. **A git worktree** of the pynchy repo at `/workspace/repos/crypdick/pynchy` — this is your normal working copy on a dedicated branch (`worktree/<group>`). Use this for all regular development.
2. **A raw mount** of the host-side clone at `/danger/raw-host-repos/crypdick/pynchy` — this gives direct access to the host's repo root (main branch, `data/`, `config.toml`, other worktrees). Use this only when you need elevated filesystem access beyond your worktree.

In short: *mac-mini* -> *host-side repo clone* -> *admin container* (worktree + raw mount).

## Container Mounts

| Container Path | What it is | Access |
|----------------|------------|--------|
| `/workspace/repos/crypdick/pynchy` | Your git worktree (branch `worktree/<group>`) | read-write |
| `/workspace/group` | `groups/{folder}/` | read-write |
| `/danger/raw-host-repos/crypdick/pynchy` | Host repo root (main branch, data/, config.toml, other worktrees) | read-write |

**Use your worktree (`/workspace/repos/crypdick/pynchy`) for normal work.** Commit and push as usual — changes sync to main via the worktree workflow.

**Use the raw mount (`/danger/...`) only when you need to:**
- Edit `config.toml` (gitignored, not present in worktrees)
- Access `data/` (messages.db, registered_groups.json, repos, worktrees)
- Read or modify other group worktrees
- Access secrets or files outside the git tree

Key paths via the raw mount:
- `/danger/raw-host-repos/crypdick/pynchy/data/messages.db` - SQLite database
- `/danger/raw-host-repos/crypdick/pynchy/data/registered_groups.json` - Group config
- `/danger/raw-host-repos/crypdick/pynchy/config.toml` - Service config (gitignored)
- `/danger/raw-host-repos/crypdick/pynchy/groups/` - All group folders

## Diagnosing Server Issues

Your worktree includes Claude Code skills (`.claude/skills/`) with procedures for diagnosing problems on the host where pynchy is running:

- **pynchy-ops** — Service management, log inspection, deploy procedures, LiteLLM proxy diagnostics, database queries. Use this for checking service status, tailing logs, investigating failed requests, and anything operational.
- **pynchy-dev** — Local development, running tests, linting, debugging agent behavior, known issues. Use this for reproducing bugs, inspecting SQLite session data, and understanding agent container behavior.

When you need to diagnose an error on mac-mini, invoke the relevant skill rather than guessing at commands. They contain tested procedures and queries.

## Managing Groups

### Finding Available Groups

Available groups are provided in `/workspace/ipc/available_groups.json`:

```json
{
  "groups": [
    {
      "folder": "family-chat",
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

**Fallback**: Query the SQLite database directly (via the raw host mount):

```bash
sqlite3 /danger/raw-host-repos/crypdick/pynchy/data/messages.db "
  SELECT jid, name, last_message_time
  FROM chats
  WHERE jid LIKE '%@g.us' AND jid != '__group_sync__'
  ORDER BY last_message_time DESC
  LIMIT 10;
"
```

### Registered Groups Config

Groups are registered in `/danger/raw-host-repos/crypdick/pynchy/data/registered_groups.json`:

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
2. Read `/danger/raw-host-repos/crypdick/pynchy/data/registered_groups.json`
3. Add the new group entry with `containerConfig` if needed
4. Write the updated JSON back
5. Create the group folder: `/danger/raw-host-repos/crypdick/pynchy/groups/{folder-name}/`

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

1. Read `/danger/raw-host-repos/crypdick/pynchy/data/registered_groups.json`
2. Remove the entry for that group
3. Write the updated JSON back
4. The group folder and its files remain (don't delete them)

### Listing Groups

Read `/danger/raw-host-repos/crypdick/pynchy/data/registered_groups.json` and format it nicely.

## Scheduled Work Status

For requests to list or audit scheduled tasks and host jobs, including paused
work, recent failures, missing next-run times, scheduler errors, or
failure-shaped result text, call `mcp__pynchy__list_tasks` first and treat its
read-only live response as authoritative. If an applicable skill must be read,
read it once, then use this native tool as the inventory source and answer
immediately from the result.

Do not use Bash, SQLite, Temporal CLI, logs, configuration files, or `/status`
to rederive the same inventory. If the native tool returns an error or states a
visibility limitation, report that bounded limitation instead of probing host
state through another path.

## Scheduling for Other Groups

When scheduling tasks for other groups, use the `target_group` parameter with the group's folder name from `registered_groups.json`:
- `schedule_task(prompt: "...", schedule_type: "cron", schedule_value: "0 9 * * 1", target_group: "family-chat")`

The task will run in that group's context with access to their files and memory.

## Self-Deploy

You can edit your own source code at `/workspace/repos/crypdick/pynchy/` and deploy changes to the running service.

### Available Tool

`deploy_changes` — optionally rebuilds the container image, restarts the service, and resumes your conversation automatically. You handle git yourself before calling this.

Parameters:
- `rebuild_container` (default: false): Set true if you changed files under `src/pynchy/agent/`
- `resume_prompt` (default: "Deploy complete. Verifying service health."): Prompt injected after restart to resume your session

### Workflow

1. Make changes to files under `/workspace/repos/crypdick/pynchy/`
2. Run tests: `uv run pytest tests/`
3. Run linter: `uv run ruff check src/`
4. Commit and push: `git add -A && git commit -m "descriptive message" && git push`
5. Deploy: call `deploy_changes`

### Safety Rules

- *Always* run tests and lint before deploying
- *Always* push before deploying — local-only commits cause divergence on restart
- Make small, focused changes — one logical change per deploy
- Write descriptive commit messages
- If you changed anything under `src/pynchy/agent/`, set `rebuild_container: true`
- After restart, verify the service is healthy before reporting success
- If the deploy causes a startup crash, the service auto-rolls back to the previous commit and resumes your session with rollback info
