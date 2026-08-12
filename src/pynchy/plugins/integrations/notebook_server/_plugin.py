"""Built-in notebook execution MCP server plugin."""

from __future__ import annotations

try:
    import pluggy

    from pynchy.plugins.api import (  # beartype resolves the hook return annotation on the host.
        McpServerSpec,
    )

    hookimpl = pluggy.HookimplMarker("pynchy")
except ModuleNotFoundError:
    # Running inside the Docker container where only the MCP server is needed,
    # not the plugin registration machinery.
    pluggy = None  # type: ignore[assignment]
    hookimpl = lambda f: f  # type: ignore[assignment]  # noqa: E731 - no-op decorator when pluggy is unavailable


class NotebookServerPlugin:
    @hookimpl
    def pynchy_mcp_server_spec(self) -> tuple[McpServerSpec, ...]:
        # Keep host-only imports inside the hook. The notebook image copies this
        # package without Pynchy so it can run the MCP server independently.
        from pynchy.plugins.api import McpServerConfig  # noqa: PLC0415

        return (
            McpServerSpec(
                name="notebook",
                config=McpServerConfig(
                    type="docker",
                    image="pynchy-mcp-notebook:latest",
                    dockerfile="src/pynchy/agent/mcp/notebook.Dockerfile",
                    build_context="src/pynchy/plugins/integrations",
                    args=["--workspace-dir", "/workspace"],
                    port=8460,
                    extra_ports=[8888],
                    transport="streamable_http",
                    idle_timeout=1800,
                    inject_workspace=True,
                    volumes=["groups/{workspace}:/workspace"],
                ),
            ),
        )
