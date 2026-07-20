# MCP-server, workspace, and job hooks

## `pynchy_mcp_server_spec`

Provide an MCP server template. Plugin templates merge with user configuration;
user definitions with the same name override plugin defaults.

```python
@hookimpl
def pynchy_mcp_server_spec(self) -> list[dict[str, Any]]:
    return [{
        "name": "gdrive",
        "type": "docker",
        "image": "pynchy-mcp-gdrive:latest",
        "dockerfile": "src/pynchy/agent/mcp/gdrive.Dockerfile",
        "port": 3100,
        "transport": "streamable_http",
    }]
```

The spec supports `docker`, `script`, and `url` servers, plus image,
Dockerfile, command, args, ports, transport, idle timeout, environment, and
volume fields. Users create instances through their tool configuration; see [MCP
servers](../../usage/mcp.md).

## `pynchy_workspace_spec`

Provide a managed workspace definition:

```python
@hookimpl
def pynchy_workspace_spec(self) -> dict[str, Any]:
    return {
        "folder": "code-improver",
        "config": {"profiles": ["code-improver"]},
    }
```

Pynchy merges plugin workspace specifications with user workspaces. `folder` is
the workspace folder name and `config` accepts `WorkspaceConfig`-compatible
fields. Deliver agent instructions through [Prompts](../../usage/prompts.md),
not a `claude_md` field.

## `pynchy_job_specs`

Provide config-backed jobs from a plugin-owned registry:

```python
@hookimpl
def pynchy_job_specs(self) -> tuple[dict[str, Any], ...]:
    return (
        {
            "name": "family-check-in",
            "config": {
                "workspace": "fam",
                "schedule": "0 15 * * *",
                "display_name": "family afternoon check-in",
                "prompt": "Review the family board and report what needs attention.",
            },
        },
    )
```

Each contribution contains `name` and a `JobConfig`-compatible `config`.
Plugin jobs use the same validation, SQLite task records, Temporal schedules,
derived-thread routing, and execution paths as `[jobs.*]`. User config wins on
name collisions. Store a logical `workspace`; do not persist a chat JID or a
generated thread folder. See [Scheduled tasks](../../usage/scheduled-tasks.md).
