"""Bounded SQLite context exposed to security decisions."""

from __future__ import annotations

import pytest

from pynchy import state


@pytest.fixture(autouse=True)
async def _setup_db() -> None:
    await state.init_test_database()


@pytest.mark.asyncio
async def test_recent_security_context_is_bounded_and_omits_tool_inputs() -> None:
    chat_jid = "security-context@test"
    for index in range(6):
        await state.store_message_direct(
            message_id=f"message-{index}",
            chat_jid=chat_jid,
            sender="user" if index % 2 == 0 else "assistant",
            sender_name="User" if index % 2 == 0 else "Assistant",
            content=f"message-{index}-" + ("x" * 600),
            timestamp=f"2026-07-19T00:00:0{index}+00:00",
            is_from_me=index % 2 == 1,
            message_type="user" if index % 2 == 0 else "assistant",
        )
    await state.store_event(
        "agent_trace",
        chat_jid,
        {"trace_type": "thinking", "tool_name": "not-an-action"},
    )
    for index in range(10):
        await state.store_event(
            "agent_trace",
            chat_jid,
            {
                "trace_type": "tool_use",
                "tool_name": f"Tool{index}",
                "tool_input": {
                    "secret": "must-not-cross-context-boundary",  # pragma: allowlist secret
                },
            },
        )

    context = await state.load_recent_security_context(chat_jid)

    assert context.current_user_intent is not None
    assert context.current_user_intent.startswith("message-4-")
    assert len(context.current_user_intent) == 500
    assert len(context.recent_messages) == 4
    assert all(len(message.content) == 500 for message in context.recent_messages)
    assert context.completed_tool_actions == tuple(f"Tool{index}" for index in range(2, 10))
    assert "must-not-cross-context-boundary" not in repr(context)
