"""Stable runtime-folder names for routed conversations."""

from __future__ import annotations

from pynchy.config.workspace_names import DYNAMIC_THREAD_DELIMITER, dynamic_thread_folder
from pynchy.conversation.models import ConversationId

_ROUTED_FRAGMENT_PREFIX = "conversation-"


def routed_conversation_folder(parent_workspace: str, conversation_id: ConversationId) -> str:
    """Return a stable child runtime folder independent of control-thread JID."""
    return dynamic_thread_folder(parent_workspace, f"{_ROUTED_FRAGMENT_PREFIX}{conversation_id}")


def conversation_id_from_folder(folder: str) -> ConversationId | None:
    """Recover routed identity from a stable child runtime folder."""
    _parent, separator, fragment = folder.partition(DYNAMIC_THREAD_DELIMITER)
    if not separator or not fragment.startswith(_ROUTED_FRAGMENT_PREFIX):
        return None
    value = fragment.removeprefix(_ROUTED_FRAGMENT_PREFIX)
    return ConversationId(value) if value.startswith("conv_") else None
