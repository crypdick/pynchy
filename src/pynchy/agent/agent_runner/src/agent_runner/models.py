"""Container I/O models — dataclasses for IPC protocol framing.

ContainerInput: parsed from initial.json in the IPC input directory at
                container start.
ContainerOutput: serialized to JSON files in the IPC output directory.

These are the container-side equivalents of the host-side types in
``pynchy.types`` — they share the same wire format but are defined
independently so the container has no dependency on the host package.
"""

from __future__ import annotations

import dataclasses
import types
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints


def _matches_hint(value: object, hint: Any) -> bool:
    """Return True if ``value`` satisfies the type ``hint``.

    Supports only the annotation forms used by ``ContainerInput``: bare classes,
    ``X | None`` unions, ``list[...]``, ``dict[..., ...]`` and ``Any``.
    """
    if hint is Any:
        return True
    origin = get_origin(hint)
    if origin is None:
        return isinstance(value, hint)
    if origin is Union or origin is types.UnionType:
        return any(_matches_hint(value, arg) for arg in get_args(hint))
    if origin is list:
        if not isinstance(value, list):
            return False
        item_hint = next(iter(get_args(hint)), Any)
        return all(_matches_hint(item, item_hint) for item in value)
    if origin is dict:
        if not isinstance(value, dict):
            return False
        args = get_args(hint)
        if not args:
            return True
        key_hint, val_hint = args
        return all(
            _matches_hint(k, key_hint) and _matches_hint(v, val_hint) for k, v in value.items()
        )
    return isinstance(value, origin)


@dataclass
class ContainerInput:
    """Parsed input received from the host via initial.json in the IPC input dir."""

    messages: list[dict[str, Any]]
    group_folder: str
    chat_jid: str
    is_admin: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    system_notices: list[str] | None = None
    repo_access: str | None = None
    agent_core_module: str = "agent_runner.cores.openai"
    agent_core_class: str = "OpenAIAgentCore"
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
        """Parse a JSON-decoded dict into a ContainerInput.

        Unknown keys are ignored; known keys are type-checked against the
        dataclass annotations so a malformed ``initial.json`` fails here at the
        boundary rather than deep in the agent core.
        """
        hints = get_type_hints(cls)
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        for name, value in kwargs.items():
            if not _matches_hint(value, hints[name]):
                raise TypeError(
                    f"ContainerInput.{name}: expected {hints[name]}, got {type(value).__name__}"
                )
        return cls(**kwargs)


@dataclass
class ContainerOutput:
    """Output sent to the host via IPC output files.

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
