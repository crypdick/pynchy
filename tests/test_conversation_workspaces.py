"""Contract coverage for stable conversation runtime folder names."""

from __future__ import annotations

from pynchy.config.workspace_names import static_workspace_name
from pynchy.conversation.workspaces import dynamic_thread_folder, parent_workspace_name


def test_dynamic_thread_folder_round_trips_to_its_parent() -> None:
    folder = dynamic_thread_folder("pynchy-dev", "discord:channel:child")

    assert folder == "pynchy-dev__thread_discord-channel-child"
    assert parent_workspace_name(folder) == "pynchy-dev"
    assert static_workspace_name(folder) == "pynchy-dev"
