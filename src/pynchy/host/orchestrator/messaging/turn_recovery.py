"""Reset handoff and interrupted-turn dispatch for the message pipeline."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable  # noqa: TC003 - beartype resolves annotations.
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - beartype resolves annotations.

from pynchy.config.settings import Settings  # noqa: TC001 - beartype resolves annotations.
from pynchy.conversation.events import new_turn_id
from pynchy.host.orchestrator.messaging.in_flight import (
    InFlightMessageDeps,
    MessageTurnStart,
    begin_message_turn,
    note_output_sent,
    resume_interrupted_message_turn,
)
from pynchy.logger import logger
from pynchy.state import (
    claim_in_flight_turn,
    clear_in_flight_turn,
    get_in_flight_turn_for_chat,
    release_in_flight_turn_claim,
)
from pynchy.types import ContainerOutput, InFlightWorkKind, WorkspaceProfile


async def handle_reset_handoff(
    deps: InFlightMessageDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    reset_file: Path,
    settings: Settings,
) -> bool | None:
    """Consume and checkpoint an agent-authored context-reset handoff."""
    if not await asyncio.to_thread(reset_file.exists):
        return None

    try:
        reset_text = await asyncio.to_thread(reset_file.read_text, encoding="utf-8")
        reset_data = json.loads(reset_text)
        await asyncio.to_thread(reset_file.unlink)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Failed to read reset prompt file",
            group=group.name,
            path=str(reset_file),
            err=str(exc),
        )
        await asyncio.to_thread(reset_file.unlink, missing_ok=True)
        return None

    reset_message = reset_data.get("message", "")
    if not reset_message:
        return True

    logger.info("Processing reset handoff", group=group.name)
    turn_id = new_turn_id()
    output_sent = False

    async def handoff_on_output(result: ContainerOutput) -> None:
        nonlocal output_sent
        sent = await deps.handle_streamed_output(chat_jid, group, result, turn_id=turn_id)
        if sent:
            await note_output_sent(turn_id, already_recorded=output_sent)
            output_sent = True

    reset_messages = [
        {
            "message_type": "user",
            "sender": "system",
            "sender_name": "System",
            "content": reset_message,
            "timestamp": datetime.now(UTC).isoformat(),
            "metadata": {"source": "reset_handoff"},
        }
    ]
    await begin_message_turn(
        MessageTurnStart(
            turn_id=turn_id,
            chat_jid=chat_jid,
            group=group,
            work_kind=InFlightWorkKind.RESET_HANDOFF,
            input_messages=reset_messages,
            input_start_cursor="",
            input_end_cursor="",
        )
    )

    try:
        result = await deps.run_agent(
            group,
            chat_jid,
            reset_messages,
            handoff_on_output,
            input_source="reset_handoff",
            turn_id=turn_id,
        )
    except asyncio.CancelledError:
        raise
    except BaseException:
        await release_in_flight_turn_claim(turn_id)
        raise

    if result == "error":
        await release_in_flight_turn_claim(turn_id)
        return False
    await clear_in_flight_turn(turn_id)

    if reset_data.get("needsDirtyRepoCheck"):
        dirty_check_file = settings.data_dir / "ipc" / group.folder / "needs_dirty_check.json"
        dirty_check_file.write_text(json.dumps({"timestamp": datetime.now(UTC).isoformat()}))
    return True


async def resume_interrupted_message_if_present(
    deps: InFlightMessageDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    process_pending: Callable[[str], Awaitable[bool]],
) -> bool | None:
    """Claim and resume the oldest interrupted message turn for one chat."""
    turn = await get_in_flight_turn_for_chat(
        chat_jid,
        {
            InFlightWorkKind.INTERACTIVE,
            InFlightWorkKind.RESET_HANDOFF,
        },
    )
    if turn is None:
        return None
    if not await claim_in_flight_turn(turn.turn_id):
        logger.info(
            "Interrupted agent turn already claimed",
            chat_jid=chat_jid,
            turn_id=turn.turn_id,
        )
        return True
    return await resume_interrupted_message_turn(
        deps,
        group,
        turn,
        process_pending,
    )
