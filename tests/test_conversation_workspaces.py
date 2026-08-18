"""Contract coverage for stable conversation runtime folder names."""

from __future__ import annotations

from pynchy.config.api import static_workspace_name
from pynchy.conversation.models import ConversationId
from pynchy.conversation.workspaces import (
    conversation_id_from_folder,
    dynamic_thread_folder,
    parent_workspace_name,
    routed_conversation_folder,
)


def test_dynamic_thread_folder_round_trips_to_its_parent() -> None:
    folder = dynamic_thread_folder("pynchy-dev", "discord:channel:child")

    assert folder == "pynchy-dev__thread_discord-channel-child"
    assert parent_workspace_name(folder) == "pynchy-dev"
    assert static_workspace_name(folder) == "pynchy-dev"


def test_routed_conversation_folder_preserves_trailing_hyphen() -> None:
    conversation_id = ConversationId("conv_token-")

    folder = routed_conversation_folder("support", conversation_id)

    assert conversation_id_from_folder(folder) == conversation_id
