"""Typed contribution objects for Pynchy plugin hooks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves dataclass annotations at runtime.
)

from pynchy.plugins.mcp_server import (  # noqa: TC001, RUF100 - beartype resolves dataclass annotations at runtime.
    McpServerConfig,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves dataclass annotations at runtime.
    ServiceTrustConfig,
)


@dataclass(frozen=True, slots=True)
class AgentCoreSpec:
    """One agent-core implementation available to the host and runner."""

    name: str
    module: str
    class_name: str
    packages: tuple[str, ...] = ()
    host_source_path: Path | None = None


@dataclass(frozen=True, slots=True)
class AgentHookSpec:
    """One trusted agent lifecycle hook module supplied by a plugin."""

    name: str
    module_path: Path


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """One named, validated MCP server template supplied by a plugin."""

    name: str
    config: McpServerConfig
    trust: ServiceTrustConfig | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """One named, validated workspace supplied by a plugin."""

    folder: str
    config: dict[str, object]


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One named, validated config-backed job supplied by a plugin."""

    name: str
    config: dict[str, object]
