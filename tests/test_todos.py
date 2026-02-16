"""Tests for pynchy.todos — host-side todo list helpers."""

from __future__ import annotations

from unittest.mock import patch

from pynchy.config import (
    AgentConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceDefaultsConfig,
)
from pynchy.todos import add_todo, get_todos


def _settings(data_dir):
    s = Settings.model_construct(
        agent=AgentConfig(),
        container=ContainerConfig(),
        server=ServerConfig(),
        logging=LoggingConfig(),
        secrets=SecretsConfig(),
        workspace_defaults=WorkspaceDefaultsConfig(),
        workspaces={},
        commands=CommandWordsConfig(),
        scheduler=SchedulerConfig(),
        intervals=IntervalsConfig(),
        queue=QueueConfig(),
        security=SecurityConfig(),
    )
    s.__dict__["data_dir"] = data_dir
    return s


class TestAddTodo:
    def test_creates_file_and_adds_item(self, tmp_path):
        with patch("pynchy.todos.get_settings", return_value=_settings(tmp_path)):
            entry = add_todo("test-group", "rename x to y")

        assert entry["content"] == "rename x to y"
        assert entry["done"] is False
        assert "id" in entry
        assert "created_at" in entry

        todos_file = tmp_path / "ipc" / "test-group" / "todos.json"
        assert todos_file.exists()

    def test_appends_to_existing_list(self, tmp_path):
        with patch("pynchy.todos.get_settings", return_value=_settings(tmp_path)):
            add_todo("test-group", "first item")
            add_todo("test-group", "second item")
            items = get_todos("test-group")

        assert len(items) == 2
        assert items[0]["content"] == "first item"
        assert items[1]["content"] == "second item"

    def test_unique_ids(self, tmp_path):
        with patch("pynchy.todos.get_settings", return_value=_settings(tmp_path)):
            a = add_todo("test-group", "a")
            b = add_todo("test-group", "b")

        assert a["id"] != b["id"]


class TestGetTodos:
    def test_returns_empty_when_no_file(self, tmp_path):
        with patch("pynchy.todos.get_settings", return_value=_settings(tmp_path)):
            items = get_todos("test-group")

        assert items == []

    def test_returns_all_items(self, tmp_path):
        with patch("pynchy.todos.get_settings", return_value=_settings(tmp_path)):
            add_todo("test-group", "item 1")
            add_todo("test-group", "item 2")
            items = get_todos("test-group")

        assert len(items) == 2

    def test_groups_are_isolated(self, tmp_path):
        with patch("pynchy.todos.get_settings", return_value=_settings(tmp_path)):
            add_todo("group-a", "item for a")
            add_todo("group-b", "item for b")

            items_a = get_todos("group-a")
            items_b = get_todos("group-b")

        assert len(items_a) == 1
        assert items_a[0]["content"] == "item for a"
        assert len(items_b) == 1
        assert items_b[0]["content"] == "item for b"
