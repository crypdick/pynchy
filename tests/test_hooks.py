"""Tests for agent_runner hooks module.

The hooks module dynamically loads Python modules from file paths and extracts
hook functions by event name. This has complex logic around import errors,
missing paths, and function discovery that warrants thorough testing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from agent_runner.hooks import HookEvent, load_hooks


class TestLoadHooks:
    """Test the load_hooks function which dynamically loads hook modules."""

    def test_empty_plugin_list(self):
        """No plugins → all events have empty lists."""
        hooks = load_hooks([])
        for event in HookEvent:
            assert hooks[event] == []

    def test_loads_single_hook_function(self, tmp_path: Path):
        """Module with a single event handler is loaded correctly."""
        hook_file = tmp_path / "test_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            def before_compact(input_data, tool_use_id, context):
                return {}
            """)
        )
        hooks = load_hooks([{"name": "test", "module_path": str(hook_file)}])
        assert len(hooks[HookEvent.BEFORE_COMPACT]) == 1
        assert callable(hooks[HookEvent.BEFORE_COMPACT][0])
        # Other events should be empty
        assert len(hooks[HookEvent.AFTER_COMPACT]) == 0
        assert len(hooks[HookEvent.ERROR]) == 0

    def test_loads_multiple_hook_functions_from_one_module(self, tmp_path: Path):
        """Module with multiple event handlers are all discovered."""
        hook_file = tmp_path / "multi_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            def before_query(input_data, tool_use_id, context):
                return {}

            def after_query(input_data, tool_use_id, context):
                return {}

            def error(input_data, tool_use_id, context):
                return {}
            """)
        )
        hooks = load_hooks([{"name": "multi", "module_path": str(hook_file)}])
        assert len(hooks[HookEvent.BEFORE_QUERY]) == 1
        assert len(hooks[HookEvent.AFTER_QUERY]) == 1
        assert len(hooks[HookEvent.ERROR]) == 1

    def test_loads_hooks_from_multiple_plugins(self, tmp_path: Path):
        """Multiple plugin modules each contribute hooks."""
        hook_a = tmp_path / "hook_a.py"
        hook_a.write_text("def before_compact(*args): return {}\n")

        hook_b = tmp_path / "hook_b.py"
        hook_b.write_text("def before_compact(*args): return {}\n")

        hooks = load_hooks(
            [
                {"name": "a", "module_path": str(hook_a)},
                {"name": "b", "module_path": str(hook_b)},
            ]
        )
        # Both should register for the same event
        assert len(hooks[HookEvent.BEFORE_COMPACT]) == 2

    def test_skips_missing_module_path(self):
        """Spec with no module_path is skipped without crashing."""
        hooks = load_hooks([{"name": "broken"}])
        for event in HookEvent:
            assert hooks[event] == []

    def test_skips_nonexistent_file(self):
        """Spec pointing to a nonexistent file is skipped."""
        hooks = load_hooks([{"name": "ghost", "module_path": "/nonexistent/hook.py"}])
        for event in HookEvent:
            assert hooks[event] == []

    def test_skips_module_with_syntax_error(self, tmp_path: Path):
        """Module with a syntax error is skipped and doesn't crash the loader."""
        hook_file = tmp_path / "bad_syntax.py"
        hook_file.write_text("def broken(\n")  # Intentional syntax error
        hooks = load_hooks([{"name": "bad", "module_path": str(hook_file)}])
        for event in HookEvent:
            assert hooks[event] == []

    def test_skips_non_callable_attributes(self, tmp_path: Path):
        """Module attributes that match event names but aren't callable are skipped."""
        hook_file = tmp_path / "non_callable.py"
        hook_file.write_text("before_compact = 'not a function'\n")
        hooks = load_hooks([{"name": "nc", "module_path": str(hook_file)}])
        assert len(hooks[HookEvent.BEFORE_COMPACT]) == 0

    def test_module_with_no_event_handlers(self, tmp_path: Path):
        """Module that loads successfully but has no matching event handlers."""
        hook_file = tmp_path / "empty_hook.py"
        hook_file.write_text("def unrelated_function(): pass\n")
        hooks = load_hooks([{"name": "empty", "module_path": str(hook_file)}])
        for event in HookEvent:
            assert hooks[event] == []

    def test_all_event_types_discoverable(self, tmp_path: Path):
        """A module can register handlers for all event types."""
        lines = []
        for event in HookEvent:
            lines.append(f"def {event.value}(*args): return {{}}")
        hook_file = tmp_path / "all_hooks.py"
        hook_file.write_text("\n".join(lines) + "\n")
        hooks = load_hooks([{"name": "all", "module_path": str(hook_file)}])
        for event in HookEvent:
            assert len(hooks[event]) == 1, f"Missing hook for {event.value}"

    def test_default_name_for_unknown_spec(self):
        """Spec without 'name' key defaults to 'unknown'."""
        # Should not crash, just skip due to missing module_path
        hooks = load_hooks([{}])
        for event in HookEvent:
            assert hooks[event] == []

    def test_hook_function_is_actually_callable(self, tmp_path: Path):
        """Loaded hook function can be called without errors."""
        hook_file = tmp_path / "callable_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            def session_start(*args, **kwargs):
                return {"handled": True}
            """)
        )
        hooks = load_hooks([{"name": "test", "module_path": str(hook_file)}])
        result = hooks[HookEvent.SESSION_START][0]()
        assert result == {"handled": True}
