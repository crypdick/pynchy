"""Tests for the container runner."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pluggy
import pytest

from pynchy.agent_home import (
    sync_skills,
    write_settings_json,
)
from pynchy.agent_protocol.api import (
    ContainerInput,
    input_to_dict,
)
from pynchy.host.container_manager import session as session_mod
from pynchy.progress_wait import ProgressTimeoutError
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.container_runner_support import (
    FakeProcess,
    _parsed_output_with_all_fields,
    _patch_settings,
    create_session,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

_CR_CREDS = "pynchy.host.container_manager.credentials"
_CR_ORCH = "pynchy.host.container_manager.orchestrator"
_GATEWAY = "pynchy.host.container_manager.gateway"


_SETTINGS_MODULES = [
    "pynchy.host.orchestrator.workspace_config",
]

_test_settings: ContextVar[Any | None] = ContextVar("test_settings", default=None)


class TestSyncSkillsFiltering:
    """Test sync_skills with workspace_skills filtering."""

    def _create_skill(self, base: Path, name: str, tier: str) -> None:
        skill_dir = base / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ntier: {tier}\n---\n# {name}\n")

    def test_none_copies_core_only(self, tmp_path: Path):
        """workspace_skills=None copies only core-tier skills (safe default)."""
        skills_src = tmp_path / "data" / "defaults" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=None)

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser"}

    def test_core_only_filters_correctly(self, tmp_path: Path):
        """workspace_skills=["core"] copies only core-tier skills."""
        skills_src = tmp_path / "data" / "defaults" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["core"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser"}

    def test_core_plus_dev(self, tmp_path: Path):
        """workspace_skills=["core", "dev"] copies core + dev skills."""
        skills_src = tmp_path / "data" / "defaults" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["core", "dev"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser", "improver"}

    def test_core_plus_specific_name(self, tmp_path: Path):
        """workspace_skills=["core", "extra"] includes core tier + named skill."""
        skills_src = tmp_path / "data" / "defaults" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["core", "extra"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser", "extra"}

    def test_star_copies_everything(self, tmp_path: Path):
        """workspace_skills=["*"] includes all skills."""
        skills_src = tmp_path / "data" / "defaults" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["*"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser", "improver"}

    def test_plugin_skills_filtered(self, tmp_path: Path):
        """Plugin skills are also filtered by workspace_skills."""
        plugin_skill = tmp_path / "plugins" / "ext-tool"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text(
            "---\nname: ext-tool\ntier: community\n---\n# External\n"
        )

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            sync_skills(
                session_dir,
                project_root=tmp_path,
                plugin_manager=FakePM(),
                workspace_skills=["core"],
            )

        # Plugin skill is community tier, should be excluded
        assert not (session_dir / "skills" / "ext-tool").exists()

    def test_plugin_skill_included_by_name(self, tmp_path: Path):
        """Plugin skill included when referenced by name."""
        plugin_skill = tmp_path / "plugins" / "ext-tool"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text(
            "---\nname: ext-tool\ntier: community\n---\n# External\n"
        )

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            sync_skills(
                session_dir,
                project_root=tmp_path,
                plugin_manager=FakePM(),
                workspace_skills=["core", "ext-tool"],
            )

        assert (session_dir / "skills" / "ext-tool").exists()


class TestWriteSettingsJson:
    """Test settings.json generation for Claude Code sessions."""

    def test_writes_default_settings(self, tmp_path: Path):
        session_dir = tmp_path / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            write_settings_json(session_dir, project_root=tmp_path)

        settings_file = session_dir / "settings.json"
        assert settings_file.exists()
        settings = json.loads(settings_file.read_text())
        assert "env" in settings
        assert settings["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"

    def test_merges_hook_config(self, tmp_path: Path):
        """Hook settings from agent/scripts/settings.json are merged."""
        scripts_dir = tmp_path / "src" / "pynchy" / "agent" / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "Bash": [
                            {
                                "matcher": "command",
                                "pattern": "git push",
                                "hook": "/opt/pynchy/scripts/guard_git.sh",
                            }
                        ]
                    }
                }
            )
        )

        session_dir = tmp_path / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            write_settings_json(session_dir, project_root=tmp_path)

        settings = json.loads((session_dir / "settings.json").read_text())
        assert "hooks" in settings
        assert "Bash" in settings["hooks"]

    def test_survives_malformed_hook_config(self, tmp_path: Path):
        """Invalid JSON in hook settings doesn't crash — falls back gracefully."""
        scripts_dir = tmp_path / "src" / "pynchy" / "agent" / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "settings.json").write_text("not valid json {{{")

        session_dir = tmp_path / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            write_settings_json(session_dir, project_root=tmp_path)

        settings = json.loads((session_dir / "settings.json").read_text())
        # Should still have env but no hooks
        assert "env" in settings
        assert "hooks" not in settings

    def test_overwrites_existing_settings(self, tmp_path: Path):
        """Settings are regenerated on each call to pick up hook changes."""
        session_dir = tmp_path / ".claude"
        session_dir.mkdir(parents=True)
        (session_dir / "settings.json").write_text('{"stale": true}')

        with _patch_settings(tmp_path):
            write_settings_json(session_dir, project_root=tmp_path)

        settings = json.loads((session_dir / "settings.json").read_text())
        assert "stale" not in settings
        assert "env" in settings


def test_parse_container_output_reads_tool_use_fields() -> None:
    out = _parsed_output_with_all_fields()

    assert out.status == "success"
    assert out.type == "tool_use"
    assert out.thinking == "Let me think..."
    assert out.tool_name == "Read"
    assert out.tool_input == {"file_path": "/test.py"}


def test_parse_container_output_reads_system_fields() -> None:
    out = _parsed_output_with_all_fields()

    assert out.system_subtype == "compact"
    assert out.system_data == {"key": "val"}
    assert out.text == "some text"


def test_parse_container_output_reads_tool_result_fields() -> None:
    out = _parsed_output_with_all_fields()

    assert out.tool_result_id == "tr-1"
    assert out.tool_result_content == "file contents"
    assert out.tool_result_is_error is False


def test_parse_container_output_reads_result_metadata() -> None:
    out = _parsed_output_with_all_fields()

    assert out.result == "done"
    assert out.new_session_id == "s1"
    assert out.result_metadata == {"duration_ms": 1234}


class TestInputToDictEdgeCases:
    """Tests for input_to_dict with various combinations of optional fields."""

    def test_minimal_input(self):
        """Only required fields, all optionals at defaults."""
        inp = ContainerInput(
            messages=[{"content": "hi"}],
            group_folder="test",
            chat_jid="test@g.us",
            is_admin=False,
        )
        d = input_to_dict(inp)
        assert d["messages"] == [{"content": "hi"}]
        assert d["group_folder"] == "test"
        assert d["chat_jid"] == "test@g.us"
        assert d["is_admin"] is False
        # None-valued optional fields should not be present
        assert "session_id" not in d
        assert "system_notices" not in d
        assert "repo_access" not in d
        # Non-None defaults (False, strings) are included
        assert d["is_scheduled_task"] is False
        assert "agent_core_module" in d

    def test_all_optional_fields_set(self):
        """All optional fields populated should appear in dict."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=True,
            session_id="s-1",
            is_scheduled_task=True,
            system_notices=["notice 1"],
            repo_access="owner/pynchy",
        )
        d = input_to_dict(inp)
        assert d["session_id"] == "s-1"
        assert d["is_scheduled_task"] is True
        assert d["system_notices"] == ["notice 1"]
        assert d["repo_access"] == "owner/pynchy"

    def test_is_scheduled_task_false_included(self):
        """is_scheduled_task=False is included (non-None values are always included)."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            is_scheduled_task=False,
        )
        d = input_to_dict(inp)
        assert d["is_scheduled_task"] is False

    def test_repo_access_none_omitted(self):
        """repo_access=None should NOT be included."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            repo_access=None,
        )
        d = input_to_dict(inp)
        assert "repo_access" not in d

    def test_agent_core_fields_always_present(self):
        """agent_core_module and agent_core_class should always be in output."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
        )
        d = input_to_dict(inp)
        assert "agent_core_module" in d
        assert "agent_core_class" in d

    def test_agent_core_config_included_when_set(self):
        """agent_core_config should appear when not None."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            agent_core_config={"model": "opus"},
        )
        d = input_to_dict(inp)
        assert d["agent_core_config"] == {"model": "opus"}

    def test_agent_core_config_omitted_when_none(self):
        """agent_core_config=None should not appear in dict."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            agent_core_config=None,
        )
        d = input_to_dict(inp)
        assert "agent_core_config" not in d

    def test_turn_id_included_when_set(self):
        """turn_id should appear when not None."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            turn_id="turn_1",
        )
        d = input_to_dict(inp)
        assert d["turn_id"] == "turn_1"


class TestContainerSessionSignalQueryDone:
    """Tests for ContainerSession.signal_query_done() public method."""

    async def test_signal_query_done_unblocks_waiter(self):
        """A query-done pulse should complete an in-flight query."""
        session = session_mod.ContainerSession("test-group", "pynchy-test-group")

        session.signal_query_done()

        await session.wait_for_query_done(query_timeout_seconds=0.1)

    async def test_signal_query_done_clears_output_handler(self):
        """signal_query_done() should clear the active output callback."""
        session = session_mod.ContainerSession("test-group", "pynchy-test-group")
        session.set_output_handler(AsyncMock())

        session.signal_query_done()

        assert session.output_handler is None

    async def test_signal_query_done_resets_idle_timer(self):
        """A completed query should trigger idle teardown after the configured delay."""
        session = session_mod.ContainerSession("test-group", "pynchy-test-group")
        expired = asyncio.Event()
        on_idle = AsyncMock(side_effect=expired.set)

        session.set_idle_timeout(0.01)
        session.set_idle_callback(on_idle)
        session.signal_query_done()

        await asyncio.wait_for(expired.wait(), timeout=0.2)

    async def test_idle_callback_failure_still_destroys_session(self):
        """A best-effort idle reaction must not prevent session destruction."""
        session = session_mod.ContainerSession("test-group", "pynchy-test-group")
        on_idle = AsyncMock(side_effect=RuntimeError("reaction failed"))
        destroy = AsyncMock()

        session.set_idle_timeout(0.01)
        session.set_idle_callback(on_idle)
        with patch("pynchy.host.container_manager.session.destroy_session", destroy):
            session.signal_query_done()
            await asyncio.sleep(0.05)

        on_idle.assert_awaited_once()
        destroy.assert_awaited_once_with("test-group")

    async def test_signal_query_done_after_set_output_handler(self):
        """A completion pulse must detach the callback before the next turn."""
        session = session_mod.ContainerSession("test-group", "pynchy-test-group")
        handler = AsyncMock()

        session.set_output_handler(handler)
        assert session.output_handler is handler

        session.signal_query_done()
        await session.wait_for_query_done(query_timeout_seconds=0.1)
        assert session.output_handler is None


class TestContainerSessionProgressTimeout:
    """Tests for progress-aware query deadlines and waiter cleanup."""

    async def test_current_turn_progress_extends_inactivity_deadline(self):
        """Structured progress should allow a healthy query past its initial deadline."""
        session = session_mod.ContainerSession("progress-test", "pynchy-progress-test")
        session.set_output_handler(AsyncMock(), query_id="query-current")

        waiter = asyncio.create_task(session.wait_for_query_done(query_timeout_seconds=0.1))
        await asyncio.sleep(0.06)
        assert session.signal_query_progress("query-current") is True
        await asyncio.sleep(0.06)
        assert session.signal_query_done("query-current") is True

        await waiter

    async def test_no_progress_hits_inactivity_timeout(self):
        """A silent live process should still be diagnosed as wedged."""
        session = session_mod.ContainerSession("silent-test", "pynchy-silent-test")
        session.set_output_handler(AsyncMock(), query_id="query-silent")

        with pytest.raises(ProgressTimeoutError, match="inactivity") as caught:
            await session.wait_for_query_done(query_timeout_seconds=0.02)

        assert caught.value.reason == "inactivity"

    async def test_continuous_progress_still_hits_finite_hard_timeout(self):
        """A noisy loop may refresh silence but cannot run forever."""
        session = session_mod.ContainerSession("hard-cap-test", "pynchy-hard-cap-test")
        session.set_output_handler(AsyncMock(), query_id="query-noisy")
        waiter = asyncio.create_task(session.wait_for_query_done(query_timeout_seconds=0.03))

        async def keep_reporting_progress() -> None:
            while not waiter.done():
                session.signal_query_progress("query-noisy")
                await asyncio.sleep(0.005)

        progress_task = asyncio.create_task(keep_reporting_progress())
        with pytest.raises(ProgressTimeoutError, match="hard") as caught:
            await waiter
        await progress_task

        assert caught.value.reason == "hard"

    async def test_stale_prior_turn_cannot_refresh_current_query(self):
        """Delayed output from a completed turn must not mask a current wedge."""
        session = session_mod.ContainerSession("stale-test", "pynchy-stale-test")
        session.set_output_handler(AsyncMock(), query_id="query-current")

        assert session.signal_query_progress("query-prior") is False
        with pytest.raises(ProgressTimeoutError, match="inactivity"):
            await session.wait_for_query_done(query_timeout_seconds=0.02)

    async def test_cancelled_waiter_leaves_session_reusable(self):
        """Cancellation must not leave a progress waiter or mutate session state."""
        session = session_mod.ContainerSession("cancel-test", "pynchy-cancel-test")
        session.set_output_handler(AsyncMock(), query_id="query-cancelled")
        waiter = asyncio.create_task(session.wait_for_query_done(query_timeout_seconds=1.0))
        await asyncio.sleep(0)

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        session.set_output_handler(AsyncMock(), query_id="query-resumed")
        session.signal_query_done("query-resumed")
        await session.wait_for_query_done(query_timeout_seconds=0.1)


class TestGetSessionOutputHandler:
    """Tests for the module-level get_session_output_handler() function."""

    @pytest.fixture(autouse=True)
    def _patch_session_cleanup(self):
        with (
            patch("pynchy.host.container_manager.session.graceful_stop", new=AsyncMock()),
            patch(
                "pynchy.host.container_manager.session.docker_rm_force",
                new=AsyncMock(),
            ),
        ):
            yield

    async def test_returns_handler_when_session_active(self):
        """Should return the session's _on_output when an active session exists."""
        session = await create_session(
            "handler-test",
            "pynchy-handler-test",
            FakeProcess(),
            data_dir=Path("unused-data"),
            idle_timeout=0.0,
        )
        handler = AsyncMock()
        session.set_output_handler(handler)

        try:
            result = session_mod.get_session_output_handler("handler-test")
            assert result is handler
        finally:
            await session_mod.destroy_session("handler-test")

    def test_returns_none_when_no_session(self):
        """Should return None when no session exists for the group."""
        result = session_mod.get_session_output_handler("nonexistent-group")
        assert result is None

    async def test_returns_none_when_no_handler_set(self):
        """Should return None when session exists but no handler is set."""
        await create_session(
            "no-handler-test",
            "pynchy-no-handler-test",
            FakeProcess(),
            data_dir=Path("unused-data"),
            idle_timeout=0.0,
        )

        try:
            result = session_mod.get_session_output_handler("no-handler-test")
            assert result is None
        finally:
            await session_mod.destroy_session("no-handler-test")
