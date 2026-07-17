"""Resolve agent-core configuration and safely resume Codex sessions."""

from __future__ import annotations

from typing import Any

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config.settings import (
    Settings,  # noqa: TC001 - beartype resolves annotations at runtime.
)

_CODEX_SESSION_PREFIX = "codex:"


def agent_core_config_from_settings(
    settings: Settings,
    group_folder: str | None = None,
) -> dict[str, Any] | None:
    """Return the configured model and reasoning effort for an agent invocation."""
    resolved_model = settings.agent.model
    if group_folder is not None:
        resolved = workspace_config.load_resolved_config(group_folder)
        if resolved is not None and resolved.model:
            resolved_model = resolved.model

    result: dict[str, Any] = {}
    if resolved_model:
        result["model"] = resolved_model
    if settings.agent.model_reasoning_effort:
        result["model_reasoning_effort"] = settings.agent.model_reasoning_effort
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
