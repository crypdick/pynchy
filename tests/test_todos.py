"""Tests for pynchy.host.orchestrator.todos — host-side todo list helpers."""

from __future__ import annotations

import json

from pynchy.host.orchestrator.todos import add_todo


class TestAddTodo:
    def test_creates_file_and_adds_item(self, tmp_path):
        entry = add_todo(tmp_path, "test-group", "rename x to y")

        assert entry["content"] == "rename x to y"
        assert entry["done"] is False
        assert "id" in entry
        assert "created_at" in entry

        todos_file = tmp_path / "ipc" / "test-group" / "todos.json"
        assert todos_file.exists()

    def test_appends_to_existing_list(self, tmp_path):
        add_todo(tmp_path, "test-group", "first item")
        add_todo(tmp_path, "test-group", "second item")
        items = json.loads((tmp_path / "ipc/test-group/todos.json").read_text())

        assert len(items) == 2
        assert items[0]["content"] == "first item"
        assert items[1]["content"] == "second item"

    def test_unique_ids(self, tmp_path):
        a = add_todo(tmp_path, "test-group", "a")
        b = add_todo(tmp_path, "test-group", "b")

        assert a["id"] != b["id"]


class TestAddTodoAtomicWrite:
    """Tests for atomic write behavior in _write_todos."""

    def test_write_is_atomic(self, tmp_path):
        """add_todo uses atomic rename; no partial writes should be visible."""
        add_todo(tmp_path, "test-group", "item 1")

        # No .tmp files should remain after write
        todos_dir = tmp_path / "ipc" / "test-group"
        tmp_files = list(todos_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_add_todo_after_corruption_overwrites_cleanly(self, tmp_path):
        """Adding a todo when the file is corrupted should create a fresh list."""
        todos_dir = tmp_path / "ipc" / "test-group"
        todos_dir.mkdir(parents=True)
        (todos_dir / "todos.json").write_text("CORRUPTED DATA")

        # _read_todos returns [] for corrupted file, then add_todo appends
        entry = add_todo(tmp_path, "test-group", "fresh start")
        items = json.loads((todos_dir / "todos.json").read_text())

        assert len(items) == 1
        assert items[0]["content"] == "fresh start"
        assert items[0]["id"] == entry["id"]
