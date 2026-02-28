"""Tests for pynchy.host.orchestrator.todos — host-side todo list helpers."""

from __future__ import annotations

from unittest.mock import patch

from conftest import make_settings

from pynchy.host.orchestrator.todos import add_todo, get_todos


class TestAddTodo:
    def test_creates_file_and_adds_item(self, tmp_path):
        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            entry = add_todo("test-group", "rename x to y")

        assert entry["content"] == "rename x to y"
        assert entry["done"] is False
        assert "id" in entry
        assert "created_at" in entry

        todos_file = tmp_path / "ipc" / "test-group" / "todos.json"
        assert todos_file.exists()

    def test_appends_to_existing_list(self, tmp_path):
        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            add_todo("test-group", "first item")
            add_todo("test-group", "second item")
            items = get_todos("test-group")

        assert len(items) == 2
        assert items[0]["content"] == "first item"
        assert items[1]["content"] == "second item"

    def test_unique_ids(self, tmp_path):
        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            a = add_todo("test-group", "a")
            b = add_todo("test-group", "b")

        assert a["id"] != b["id"]


class TestGetTodos:
    def test_returns_empty_when_no_file(self, tmp_path):
        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            items = get_todos("test-group")

        assert items == []

    def test_returns_all_items(self, tmp_path):
        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            add_todo("test-group", "item 1")
            add_todo("test-group", "item 2")
            items = get_todos("test-group")

        assert len(items) == 2

    def test_groups_are_isolated(self, tmp_path):
        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            add_todo("group-a", "item for a")
            add_todo("group-b", "item for b")

            items_a = get_todos("group-a")
            items_b = get_todos("group-b")

        assert len(items_a) == 1
        assert items_a[0]["content"] == "item for a"
        assert len(items_b) == 1
        assert items_b[0]["content"] == "item for b"

    def test_returns_empty_on_corrupted_json(self, tmp_path):
        """Corrupted todos.json should not crash — returns empty list."""
        todos_dir = tmp_path / "ipc" / "test-group"
        todos_dir.mkdir(parents=True)
        (todos_dir / "todos.json").write_text("not valid json {{{")

        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            items = get_todos("test-group")

        assert items == []

    def test_returns_empty_on_empty_file(self, tmp_path):
        """Empty todos.json should not crash — returns empty list."""
        todos_dir = tmp_path / "ipc" / "test-group"
        todos_dir.mkdir(parents=True)
        (todos_dir / "todos.json").write_text("")

        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            items = get_todos("test-group")

        assert items == []


class TestAddTodoAtomicWrite:
    """Tests for atomic write behavior in _write_todos."""

    def test_write_is_atomic(self, tmp_path):
        """add_todo uses atomic rename; no partial writes should be visible."""
        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            add_todo("test-group", "item 1")

        # No .tmp files should remain after write
        todos_dir = tmp_path / "ipc" / "test-group"
        tmp_files = list(todos_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_add_todo_after_corruption_overwrites_cleanly(self, tmp_path):
        """Adding a todo when the file is corrupted should create a fresh list."""
        todos_dir = tmp_path / "ipc" / "test-group"
        todos_dir.mkdir(parents=True)
        (todos_dir / "todos.json").write_text("CORRUPTED DATA")

        with patch("pynchy.host.orchestrator.todos.get_settings", return_value=make_settings(data_dir=tmp_path)):
            # _read_todos returns [] for corrupted file, then add_todo appends
            entry = add_todo("test-group", "fresh start")
            items = get_todos("test-group")

        assert len(items) == 1
        assert items[0]["content"] == "fresh start"
        assert items[0]["id"] == entry["id"]
