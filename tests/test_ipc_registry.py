"""Tests for agent_runner.registry — agent core instantiation and error handling.

The registry is responsible for dynamically importing and instantiating agent core
implementations. It has non-trivial error handling across 3 distinct failure modes
(import, attribute lookup, instantiation) and a runtime protocol check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner.core import AgentCore, AgentCoreConfig  # noqa: E402
from agent_runner.registry import create_agent_core  # noqa: E402


def _make_config(**overrides: object) -> AgentCoreConfig:
    defaults = {
        "cwd": "/workspace/project",
        "session_id": None,
        "group_folder": "test",
        "chat_jid": "test@g.us",
        "is_admin": False,
        "is_scheduled_task": False,
    }
    defaults.update(overrides)
    return AgentCoreConfig(**defaults)


# ---------------------------------------------------------------------------
# Import failures
# ---------------------------------------------------------------------------


class TestImportFailures:
    """Verify descriptive errors when the module can't be imported."""

    def test_nonexistent_module_raises_import_error(self):
        """Completely made-up module path should raise ImportError with context."""
        config = _make_config()
        with pytest.raises(ImportError, match="Failed to import.*nonexistent"):
            create_agent_core("nonexistent.module.path", "SomeClass", config)

    def test_import_error_preserves_original_cause(self):
        """The chained __cause__ should be the original ImportError."""
        config = _make_config()
        with pytest.raises(ImportError) as exc_info:
            create_agent_core("no_such_module_xyz", "Anything", config)
        assert exc_info.value.__cause__ is not None

    def test_partial_module_path_raises_import_error(self):
        """A module path where only the parent exists should still raise ImportError."""
        config = _make_config()
        with pytest.raises(ImportError, match="Failed to import"):
            create_agent_core("agent_runner.cores.nonexistent_core", "FakeCore", config)


# ---------------------------------------------------------------------------
# Attribute lookup failures
# ---------------------------------------------------------------------------


class TestAttributeFailures:
    """Verify descriptive errors when the class doesn't exist in the module."""

    def test_nonexistent_class_raises_attribute_error(self):
        """Existing module but wrong class name should raise AttributeError."""
        config = _make_config()
        with pytest.raises(AttributeError, match="has no class.*DoesNotExist"):
            create_agent_core("agent_runner.core", "DoesNotExist", config)

    def test_attribute_error_includes_module_name(self):
        """Error message should include the module path for debugging."""
        config = _make_config()
        with pytest.raises(AttributeError, match="agent_runner.core"):
            create_agent_core("agent_runner.core", "MissingClass", config)

    def test_attribute_error_preserves_original_cause(self):
        """The chained __cause__ should be the original AttributeError."""
        config = _make_config()
        with pytest.raises(AttributeError) as exc_info:
            create_agent_core("agent_runner.core", "NeverDefined", config)
        assert exc_info.value.__cause__ is not None


# ---------------------------------------------------------------------------
# Instantiation failures
# ---------------------------------------------------------------------------


class TestInstantiationFailures:
    """Verify descriptive errors when the constructor fails."""

    def test_class_with_bad_constructor_raises_type_error(self, monkeypatch: pytest.MonkeyPatch):
        """If the class constructor raises, we get a TypeError with context."""

        class BrokenCore:
            def __init__(self, config: AgentCoreConfig) -> None:
                raise RuntimeError("constructor exploded")

        # Temporarily inject our broken class into the agent_runner.core module
        import agent_runner.core as core_mod

        monkeypatch.setattr(core_mod, "BrokenCore", BrokenCore, raising=False)

        config = _make_config()
        with pytest.raises(TypeError, match="Failed to instantiate.*BrokenCore"):
            create_agent_core("agent_runner.core", "BrokenCore", config)

    def test_instantiation_error_preserves_original_cause(self, monkeypatch: pytest.MonkeyPatch):
        """The chained __cause__ should be the original exception from __init__."""

        class ExplodingCore:
            def __init__(self, config: AgentCoreConfig) -> None:
                raise ValueError("bad config value")

        import agent_runner.core as core_mod

        monkeypatch.setattr(core_mod, "ExplodingCore", ExplodingCore, raising=False)

        config = _make_config()
        with pytest.raises(TypeError) as exc_info:
            create_agent_core("agent_runner.core", "ExplodingCore", config)
        assert isinstance(exc_info.value.__cause__, ValueError)


# ---------------------------------------------------------------------------
# Protocol check
# ---------------------------------------------------------------------------


class TestProtocolCheck:
    """Verify runtime AgentCore protocol compliance."""

    def test_non_protocol_class_raises_type_error(self, monkeypatch: pytest.MonkeyPatch):
        """A class that doesn't satisfy AgentCore protocol should be rejected."""

        class NotAnAgentCore:
            """Has __init__ but lacks start/query/stop/session_id."""

            def __init__(self, config: AgentCoreConfig) -> None:
                pass

        import agent_runner.core as core_mod

        monkeypatch.setattr(core_mod, "NotAnAgentCore", NotAnAgentCore, raising=False)

        config = _make_config()
        with pytest.raises(TypeError, match="does not satisfy AgentCore protocol"):
            create_agent_core("agent_runner.core", "NotAnAgentCore", config)

    def test_valid_protocol_class_succeeds(self, monkeypatch: pytest.MonkeyPatch):
        """A class that satisfies AgentCore protocol should instantiate successfully."""

        class FakeCore:
            def __init__(self, config: AgentCoreConfig) -> None:
                self._session_id = None

            async def start(self) -> None:
                pass

            async def query(self, prompt: str):
                yield  # pragma: no cover

            async def stop(self) -> None:
                pass

            @property
            def session_id(self) -> str | None:
                return self._session_id

        import agent_runner.core as core_mod

        monkeypatch.setattr(core_mod, "FakeCore", FakeCore, raising=False)

        config = _make_config()
        core = create_agent_core("agent_runner.core", "FakeCore", config)
        assert isinstance(core, AgentCore)
        assert core.session_id is None


# ---------------------------------------------------------------------------
# Config forwarding
# ---------------------------------------------------------------------------


class TestConfigForwarding:
    """Verify the config object is passed through correctly."""

    def test_config_is_forwarded_to_constructor(self, monkeypatch: pytest.MonkeyPatch):
        """The AgentCoreConfig should be passed as the sole argument."""
        received_config = None

        class InspectorCore:
            def __init__(self, config: AgentCoreConfig) -> None:
                nonlocal received_config
                received_config = config
                self._session_id = config.session_id

            async def start(self) -> None:
                pass

            async def query(self, prompt: str):
                yield  # pragma: no cover

            async def stop(self) -> None:
                pass

            @property
            def session_id(self) -> str | None:
                return self._session_id

        import agent_runner.core as core_mod

        monkeypatch.setattr(core_mod, "InspectorCore", InspectorCore, raising=False)

        config = _make_config(session_id="sess-42", is_admin=True, group_folder="admin-1")
        create_agent_core("agent_runner.core", "InspectorCore", config)

        assert received_config is not None
        assert received_config.session_id == "sess-42"
        assert received_config.is_admin is True
        assert received_config.group_folder == "admin-1"
