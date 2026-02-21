"""Container I/O models — dataclasses for stdin/stdout protocol framing.

ContainerInput: parsed from JSON on stdin at container start.
ContainerOutput: serialized to JSON on stdout, wrapped in output markers.

These are the container-side equivalents of the host-side types in
``pynchy.types`` — they share the same wire format but are defined
independently so the container has no dependency on the host package.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any


@dataclass
class ContainerInput:
    """Parsed input received from the host via stdin JSON."""

    messages: list[dict[str, Any]]
    group_folder: str
    chat_jid: str
    is_admin: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    system_notices: list[str] | None = None
    repo_access: str | None = None
    agent_core_module: str = "agent_runner.cores.claude"
    agent_core_class: str = "ClaudeAgentCore"
    agent_core_config: dict[str, Any] | None = None
    system_prompt_append: str | None = None
    mcp_gateway_url: str | None = None
    mcp_gateway_key: str | None = None
    mcp_direct_servers: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        # Normalize empty string to None (JSON has no null distinction for
        # missing-vs-empty in TOML, and the host may send "" for unset).
        if self.repo_access == "":
            self.repo_access = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContainerInput:
        """Create from a JSON-parsed dict, ignoring unknown keys."""
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ContainerOutput:
    """Output sent to the host via stdout JSON.

    The ``type`` field controls which subset of fields are serialized
    by ``to_dict()`` — only fields relevant to the event type are included.
    """

    status: str
    result: str | None = None
    new_session_id: str | None = None
    error: str | None = None
    type: str = "result"
    thinking: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    text: str | None = None
    system_subtype: str | None = None
    system_data: dict[str, Any] | None = None
    tool_result_id: str | None = None
    tool_result_content: str | None = None
    tool_result_is_error: bool | None = None
    result_metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON output.

        Only includes fields relevant to ``self.type`` to keep the wire
        format compact.  The ``type`` and ``status`` fields are always present.
        """
        d: dict[str, Any] = {"type": self.type, "status": self.status}

        if self.type == "result":
            d["result"] = self.result
            if self.new_session_id:
                d["new_session_id"] = self.new_session_id
            if self.error:
                d["error"] = self.error
            if self.result_metadata:
                d["result_metadata"] = self.result_metadata
        elif self.type == "thinking":
            d["thinking"] = self.thinking
        elif self.type == "tool_use":
            d["tool_name"] = self.tool_name
            d["tool_input"] = self.tool_input
        elif self.type == "text":
            d["text"] = self.text
        elif self.type == "system":
            d["system_subtype"] = self.system_subtype
            d["system_data"] = self.system_data
        elif self.type == "tool_result":
            d["tool_result_id"] = self.tool_result_id
            d["tool_result_content"] = self.tool_result_content
            d["tool_result_is_error"] = self.tool_result_is_error

        return d
