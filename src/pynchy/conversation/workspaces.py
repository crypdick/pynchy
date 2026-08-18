"""Stable runtime-folder names for routed conversations."""

from __future__ import annotations

import re

from pynchy.conversation.models import ConversationId

_DYNAMIC_THREAD_DELIMITER = "__thread_"
_ROUTED_FRAGMENT_PREFIX = "conversation-"


def dynamic_thread_folder(parent_folder: str, thread_jid: str) -> str:
    """Return the stable runtime folder for a child conversation."""
    fragment = re.sub(r"[^A-Za-z0-9_.-]+", "-", thread_jid).strip("-")
    return f"{parent_folder}{_DYNAMIC_THREAD_DELIMITER}{fragment or 'thread'}"


def parent_workspace_name(workspace_name: str) -> str | None:
    """Return the configured parent for a generated child runtime folder."""
    parent, delimiter, _thread = workspace_name.partition(_DYNAMIC_THREAD_DELIMITER)
    return parent if delimiter and parent else None


def routed_conversation_folder(parent_workspace: str, conversation_id: ConversationId) -> str:
    """Return a reversible child folder independent of control-thread JID."""
    return (
        f"{parent_workspace}{_DYNAMIC_THREAD_DELIMITER}{_ROUTED_FRAGMENT_PREFIX}{conversation_id}"
    )


def conversation_id_from_folder(folder: str) -> ConversationId | None:
    """Recover routed identity from a stable child runtime folder."""
    _parent, separator, fragment = folder.partition(_DYNAMIC_THREAD_DELIMITER)
    if not separator or not fragment.startswith(_ROUTED_FRAGMENT_PREFIX):
        return None
    value = fragment.removeprefix(_ROUTED_FRAGMENT_PREFIX)
    return ConversationId(value) if value.startswith("conv_") else None
