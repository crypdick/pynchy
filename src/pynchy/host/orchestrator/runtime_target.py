"""Stable runtime identity and replaceable presentation binding."""

from __future__ import annotations

from dataclasses import dataclass

from pynchy.types import ChatJid, GroupFolder, RuntimeId, WorkspaceProfile


@dataclass(frozen=True, slots=True)
class RuntimeTarget:
    """One execution runtime and its current human-facing control address."""

    folder: GroupFolder
    chat_jid: ChatJid

    @property
    def id(self) -> RuntimeId:
        """Return the stable queue identity derived from the runtime folder."""
        return RuntimeId(self.folder)

    @classmethod
    def from_workspace(cls, workspace: WorkspaceProfile) -> RuntimeTarget:
        """Build the runtime target currently represented by a workspace profile."""
        return cls.from_binding(workspace.folder, workspace.jid)

    @classmethod
    def from_binding(cls, folder: str, chat_jid: str) -> RuntimeTarget:
        """Build a target from independently resolved runtime and control concerns."""
        group_folder = GroupFolder(folder)
        return cls(
            folder=group_folder,
            chat_jid=ChatJid(chat_jid),
        )
