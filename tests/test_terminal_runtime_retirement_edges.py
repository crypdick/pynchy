"""Terminal runtime retirement boundary contracts."""

from __future__ import annotations

from asyncio import sleep

from pynchy.conversation.api import ConversationId, TerminalConversationRetirement
from pynchy.host.orchestrator.webhook_terminal_retirement import retire_terminal_runtime


class _RetirementDeps:
    async def unregister_workspace(self, _jid: str) -> None:
        await sleep(0)

    async def retire_conversation_runtime(self, _folder: str) -> None:
        await sleep(0)

    async def retire_conversation_tasks(self, _conversation_id: ConversationId) -> None:
        await sleep(0)

    async def conversation_control_state_matches(
        self,
        _conversation_id: ConversationId,
        *,
        closed: bool,
        control_state_revision: str | None,
        delivery_identity=None,
        claim_id=None,
    ) -> bool:
        del closed, control_state_revision, delivery_identity, claim_id
        await sleep(0)
        return False


async def test_stale_terminal_retirement_does_not_touch_runtime() -> None:
    retirement = TerminalConversationRetirement(
        runtime_folders=(),
        runtime_workspace_jids=(),
        is_current=False,
    )

    assert (
        await retire_terminal_runtime(
            _RetirementDeps(), ConversationId("conversation"), retirement, set()
        )
        is False
    )


async def test_terminal_retirement_requires_matching_control_state() -> None:
    retirement = TerminalConversationRetirement(
        runtime_folders=(),
        runtime_workspace_jids=(),
    )

    assert (
        await retire_terminal_runtime(
            _RetirementDeps(), ConversationId("conversation"), retirement, set()
        )
        is False
    )
