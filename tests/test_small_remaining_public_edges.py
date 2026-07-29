"""Small public edge contracts left by the coverage report."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pynchy.config.api import JobConfig
from pynchy.host.orchestrator.messaging.cursor import advance_cursor
from pynchy.host.orchestrator.runtime_registry import RuntimeRegistry
from pynchy.identifiers import RuntimeId
from pynchy.plugins.integrations.playwright_browser import PlaywrightBrowserPlugin


@dataclass
class _CursorDeps:
    last_agent_timestamp: dict[str, str] = field(default_factory=dict)
    should_fail: bool = True

    async def save_state(self) -> None:
        if self.should_fail:
            raise RuntimeError("state unavailable")


@pytest.mark.asyncio
async def test_cursor_advance_rolls_back_when_state_persistence_fails() -> None:
    deps = _CursorDeps(last_agent_timestamp={"chat": "old"})

    with pytest.raises(RuntimeError, match="state unavailable"):
        await advance_cursor(deps, "chat", "new")

    assert deps.last_agent_timestamp == {"chat": "old"}


def test_runtime_registry_reports_a_missing_runtime() -> None:
    with pytest.raises(RuntimeError, match="has not entered the queue"):
        RuntimeRegistry().require(RuntimeId("missing"))


def test_job_config_rejects_blank_workspace_text() -> None:
    assert JobConfig.validate_workspace(None) is None

    with pytest.raises(ValueError, match="workspace cannot be empty"):
        JobConfig(schedule="0 * * * *", workspace="   ", prompt="check")

    with pytest.raises(ValueError, match="require workspace"):
        JobConfig(schedule="0 * * * *", prompt="check")


def test_playwright_plugin_returns_no_skill_path_when_directory_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.playwright_browser._plugin.Path.is_dir",
        lambda _path: False,
    )

    assert PlaywrightBrowserPlugin().pynchy_skill_paths() == []
