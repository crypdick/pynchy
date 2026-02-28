"""Answer delivery for the ask_user flow.

Routes a user's answer back to the waiting container (if alive) or
injects it as a synthetic message for cold-start (if the container died
while waiting).

Two paths:
  Path A (container alive):
    Write the answer as an IPC response file.  The container's watchdog
    picks it up and unblocks the pending ask_user call.

  Path B (container dead):
    Format the Q&A as a context message and enqueue it through the
    message pipeline, which triggers a cold-start with the answer.

See docs/plans/2026-02-22-ask-user-blocking-design.md
"""

from __future__ import annotations

from typing import Any, Protocol

from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.session import get_session
from pynchy.host.orchestrator.messaging.pending_questions import (
    find_pending_question,
    resolve_pending_question,
)
from pynchy.logger import logger


class AskUserDeps(Protocol):
    """Dependencies for ask_user answer delivery.

    The App class satisfies this protocol — it already has message
    ingestion via _on_inbound.
    """

    async def enqueue_message(self, chat_jid: str, text: str) -> None: ...


async def handle_ask_user_answer(
    request_id: str,
    answer: dict[str, Any],
    deps: AskUserDeps,
) -> None:
    """Route a user's answer to the waiting container or cold-start if dead."""
    pending = find_pending_question(request_id)
    if pending is None:
        logger.warning("Answer for unknown question", request_id=request_id)
        return

    source_group = pending["source_group"]
    session = get_session(source_group)

    if session is not None and session.is_alive:
        # Path A: container alive -- write IPC response file
        path = ipc_response_path(source_group, request_id)
        try:
            write_ipc_response(path, {"result": {"answers": answer}})
            logger.info(
                "ask_user answer delivered via IPC",
                request_id=request_id,
                source_group=source_group,
            )
        except OSError:
            logger.exception(
                "Failed to write IPC response, falling back to cold-start",
                request_id=request_id,
                source_group=source_group,
            )
            answer_text = _format_answer_context(pending, answer)
            await deps.enqueue_message(pending["chat_jid"], answer_text)
    else:
        # Path B: container dead -- cold-start with answer context
        answer_text = _format_answer_context(pending, answer)
        await deps.enqueue_message(pending["chat_jid"], answer_text)
        logger.info(
            "ask_user answer enqueued for cold-start",
            request_id=request_id,
            source_group=source_group,
        )

    resolve_pending_question(request_id, source_group)


def _format_answer_context(pending: dict, answer: dict[str, Any]) -> str:
    """Format the Q&A as context text for cold-start message injection.

    Produces text like:
        You previously asked the user: "Which auth strategy?"
        Options: 1. JWT tokens, 2. Session cookies, 3. OAuth 2.0
        The user answered: "JWT tokens"
        Continue from where you left off.
    """
    parts: list[str] = []

    for q in pending.get("questions", []):
        question_text = q.get("question", "")
        parts.append(f'You previously asked the user: "{question_text}"')

        options = q.get("options")
        if options:
            labels = [
                opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt) for opt in options
            ]
            numbered = ", ".join(f"{i}. {lbl}" for i, lbl in enumerate(labels, 1))
            parts.append(f"Options: {numbered}")

    # Format the answer dict as readable text
    if len(answer) == 1:
        # Single answer -- just show the value
        val = next(iter(answer.values()))
        parts.append(f'The user answered: "{val}"')
    else:
        # Multiple answers -- show key: value pairs
        answer_lines = "; ".join(f"{k}: {v}" for k, v in answer.items())
        parts.append(f"The user answered: {answer_lines}")

    parts.append("Continue from where you left off.")

    return "\n".join(parts)
