"""Resolve agent-core configuration and safely resume Codex sessions."""

from __future__ import annotations

from typing import Any

import pynchy.host.orchestrator.workspace_config as workspace_config

_CODEX_SESSION_PREFIX = "codex:"


def agent_core_config(
    model: str | None,
    model_reasoning_effort: str | None,
    group_folder: str | None = None,
) -> dict[str, Any] | None:
    """Return the configured model and reasoning effort for an agent invocation."""
    resolved_model = model
    resolved_reasoning_effort = model_reasoning_effort
    if group_folder is not None:
        resolved = workspace_config.load_resolved_config(group_folder)
        if resolved is not None:
            if resolved.model:
                resolved_model = resolved.model
            if resolved.model_reasoning_effort:
                resolved_reasoning_effort = resolved.model_reasoning_effort

    result: dict[str, Any] = {}
    if resolved_model:
        result["model"] = resolved_model
    if resolved_reasoning_effort:
        result["model_reasoning_effort"] = resolved_reasoning_effort
    return result or None


def session_model_mismatch(
    session_id: str | None,
    agent_core_config: dict[str, Any] | None,
) -> bool:
    """Return whether a saved Codex thread was created for another model."""
    if not session_id or not session_id.startswith(_CODEX_SESSION_PREFIX):
        return False
    model, separator, _thread_id = session_id.removeprefix(_CODEX_SESSION_PREFIX).partition(":")
    stored_model = model if separator else None
    return stored_model != (agent_core_config or {}).get("model")
