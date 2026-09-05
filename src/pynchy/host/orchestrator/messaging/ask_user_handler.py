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

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves callback annotations.
)
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.host.orchestrator.messaging.pending_questions import (
    find_pending_question,
    resolve_pending_question,
)
from pynchy.identifiers import GroupFolder
from pynchy.logger import logger


@dataclass
class AskUserRuntimeOperations:
    """Container effects selected by the application composition root."""

    has_live_session: Callable[[GroupFolder], bool]
    persist_skill_access: Callable[[dict[str, Any], dict[str, Any]], str | None]
    write_response: Callable[[str, str, dict[str, object]], None]


@runtime_checkable
class AskUserDeps(Protocol):
    """Answer delivery and cold-start enqueue capabilities supplied by the app."""

    async def enqueue_message(self, chat_jid: str, text: str) -> None: ...

    def has_active_host_process(self, group_folder: str) -> bool: ...

    @property
    def ask_user_runtime_operations(self) -> AskUserRuntimeOperations: ...


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
    operations = deps.ask_user_runtime_operations
    try:
        skill_access_status = operations.persist_skill_access(pending, answer)
    except (OSError, ValueError) as exc:
        logger.warning("Could not persist skill access choice", request_id=request_id, err=str(exc))
        skill_access_status = "error"

    if operations.has_live_session(GroupFolder(source_group)) or deps.has_active_host_process(
        source_group
    ):
        # Path A: container or direct host process alive -- write IPC response file.
        try:
            result: dict[str, object] = {"answers": answer}
            if skill_access_status is not None:
                result["skill_access_status"] = skill_access_status
            operations.write_response(source_group, request_id, result)
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


def _format_answer_context(pending: dict[str, Any], answer: dict[str, Any]) -> str:
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
