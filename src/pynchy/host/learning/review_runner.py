"""Hidden Obsidian learning review execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pynchy.host.learning.paths import resolve_learning_paths
from pynchy.host.learning.reviewer import build_review_prompt, should_review
from pynchy.learning_packets import (
    LearningPacket,
)
from pynchy.workspace.api import WorkspaceProfile

RunAgent = Callable[..., Awaitable[str]]
_LEARNING_PATHS_UNAVAILABLE_ERROR = (
    "learning paths unavailable for group {group_folder!r} profile {profile!r}"
)
_LEARNING_REVIEWER_RESULT_ERROR = "learning reviewer returned {result!r}"


async def run_learning_review(
    packet: LearningPacket,
    run_agent: RunAgent,
    reviewer_prompt: str,
) -> str:
    """Run the hidden reviewer agent for one learning packet."""
    if not should_review(packet):
        return "skipped"

    paths = resolve_learning_paths(packet.group_folder, profile_override=packet.profile)
    if paths is None:
        raise RuntimeError(
            _LEARNING_PATHS_UNAVAILABLE_ERROR.format(
                group_folder=packet.group_folder,
                profile=packet.profile,
            )
        )

    async def on_output(_output: object) -> None:  # noqa: RUF029 - run_agent expects an async output callback.
        return None

    reviewer_jid = f"learning-review:{paths.profile_slug}"
    result = await run_agent(
        WorkspaceProfile(
            jid=reviewer_jid,
            name="Learning Reviewer",
            folder=f"learning-review-{paths.profile_slug}",
            trigger="",
            is_admin=False,
        ),
        reviewer_jid,
        [{"role": "user", "content": build_review_prompt(packet, paths, reviewer_prompt)}],
        on_output=on_output,
        extra_system_notices=None,
        is_scheduled_task=True,
        repo_access_override=None,
        input_source="hidden_learning_review",
    )
    if result == "success":
        return "completed"
    raise RuntimeError(_LEARNING_REVIEWER_RESULT_ERROR.format(result=result))
