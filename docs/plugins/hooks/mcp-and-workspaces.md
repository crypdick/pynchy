# MCP Server, Workspace, and Job Hooks

## `pynchy_mcp_server_spec`

Provide validated MCP server templates. Plugin templates merge with user
configuration; user definitions with the same name override plugin defaults.

```python
from pynchy.plugins.mcp_server import McpServerConfig
from pynchy.plugins.contracts import McpServerSpec
from pynchy.workspace.api import ServiceTrustConfig


@hookimpl
def pynchy_mcp_server_spec(self) -> tuple[McpServerSpec, ...]:
    return (
        McpServerSpec(
            name="gdrive",
            config=McpServerConfig(
                type="docker",
                image="pynchy-mcp-gdrive:latest",
                dockerfile="src/pynchy/agent/mcp/gdrive.Dockerfile",
                build_context="src/pynchy/agent/mcp",
                port=3100,
                transport="streamable_http",
            ),
            trust=ServiceTrustConfig(public_source=True, secret_data=True),
        ),
    )
```

`McpServerConfig` supports `docker`, `script`, `stdio`, and `url` servers,
plus image, Dockerfile, build context, command, arguments, ports, transport,
idle timeout, environment, and volume fields. `stdio` wraps a trusted host command in a
loopback HTTP bridge. Optional `ServiceTrustConfig` declares the template's
trust defaults separately from its runtime configuration. Users create
instances through their tool configuration. That tool owns credential
requirements and companion skills; plugin server templates cannot expose host
credentials by themselves. See [MCP servers](../../usage/mcp.md) and
[Tool access and secrets](../../usage/tool-access.md).

## `pynchy_workspace_spec`

Provide a managed workspace definition:

```python
from pynchy.config.models import WorkspaceConfig
from pynchy.plugins.contracts import WorkspaceSpec


@hookimpl
def pynchy_workspace_spec(self) -> WorkspaceSpec:
    return WorkspaceSpec(
        folder="code-improver",
        config=WorkspaceConfig(profiles=["code-improver"]),
    )
```

Pynchy merges plugin workspace specifications with user workspaces. `folder` is
the workspace folder name, and `config` is an already validated
`WorkspaceConfig`. Deliver agent instructions through
[Prompts](../../usage/prompts.md), not a `claude_md` field.

## `pynchy_job_specs`

Provide config-backed jobs from a plugin-owned registry:

```python
from pynchy.config.jobs import JobConfig
from pynchy.plugins.contracts import JobSpec


@hookimpl
def pynchy_job_specs(self) -> tuple[JobSpec, ...]:
    return (
        JobSpec(
            name="family-check-in",
            config=JobConfig(
                workspace="fam",
                schedule="0 15 * * *",
                display_name="family afternoon check-in",
                prompt="Review the family board and report what needs attention.",
            ),
        ),
    )
```

Plugin jobs enter the same `JobConfig` registry, validation, SQLite task
records, Temporal schedules, derived-thread routing, and execution paths as
file-backed automations. Personalized config wins on name collisions. Store a
logical `workspace`; do not persist a chat JID or generated thread folder. See
[Scheduled tasks](../../usage/scheduled-tasks.md).
