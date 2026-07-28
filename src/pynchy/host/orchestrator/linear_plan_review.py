"""Hidden agent review of approved Linear plans before execution admission."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pynchy.agent_protocol.api import ContainerOutput
    from pynchy.host.orchestrator.concurrency import GroupQueue

from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspaceRestriction,
    register_runtime_workspace_restriction,
)
from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewRequest,
    LinearPlanReviewResult,
)
from pynchy.logger import logger
from pynchy.workspace.api import (
    CapabilityRule,
    RuntimeTarget,
    WorkspaceProfile,
)

_REVIEWER_RESULT_ERROR = "Plan reviewer did not return one valid JSON decision"
_REVIEW_PROMPT = """\
You are Pynchy's independent Linear plan-freshness reviewer. Work read-only.
Inspect the current repositories and relevant documentation, then decide whether
the already approved plan remains sane at the current repository state.

Use your judgment. Normal implementation discretion and minor drift are reasons
to proceed, not reasons to create planning ceremony. Request replanning only when
stale assumptions, changed architecture, completed work, or changed requirements
make the approved plan materially wrong.

Do not edit files, call mutating tools, publish work, or modify external systems.
Return exactly one JSON object and no Markdown:

{"decision":"proceed","reason":"brief evidence-based explanation"}

or:

{"decision":"replan","reason":"brief evidence-based explanation",\
"plan":"complete replacement Markdown plan"}

Current Linear issue:
"""


@runtime_checkable
class PlanReviewDeps(Protocol):
    """Host capabilities needed by one hidden plan review."""

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def run_agent(  # noqa: PLR0913, RUF100 - mirrors the orchestrator contract.
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict[str, object]],
        on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        repo_access_override: str | None = None,
        input_source: str = "user",
    ) -> str: ...


def _reviewer_profile(request: LinearPlanReviewRequest) -> WorkspaceProfile:
    digest = hashlib.sha256(request.issue_id.encode()).hexdigest()[:16]
    folder = f"linear-plan-review-{digest}"
    register_runtime_workspace_restriction(
        folder,
        RuntimeWorkspaceRestriction(
            parent_workspace=request.workspace,
            tools=(),
            capabilities={"*": CapabilityRule(decision="deny")},
        ),
    )
    return WorkspaceProfile(
        jid=f"linear-plan-review:{request.issue_id}",
        name=f"{request.identifier} Plan Reviewer",
        folder=folder,
        trigger="",
        is_admin=False,
    )


def _review_prompt(request: LinearPlanReviewRequest) -> str:
    issue = {
        "id": request.issue_id,
        "identifier": request.identifier,
        "title": request.title,
        "url": request.url,
        "updated_at": request.updated_at,
        "description": request.description,
    }
    return f"{_REVIEW_PROMPT}\n{json.dumps(issue, ensure_ascii=False, indent=2)}"


def _parse_result(raw: str) -> LinearPlanReviewResult:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise TypeError(_REVIEWER_RESULT_ERROR)
    raw_decision = payload.get("decision")
    reason = payload.get("reason")
    plan = payload.get("plan")
    if not isinstance(raw_decision, str) or not isinstance(reason, str):
        raise TypeError(_REVIEWER_RESULT_ERROR)
    if plan is not None and not isinstance(plan, str):
        raise TypeError(_REVIEWER_RESULT_ERROR)
    return LinearPlanReviewResult(
        decision=LinearPlanReviewDecision(raw_decision),
        reason=reason,
        plan=plan,
    )


async def _run_queued_review(
    deps: PlanReviewDeps,
    request: LinearPlanReviewRequest,
) -> LinearPlanReviewResult:
    group = _reviewer_profile(request)

    async def run_review() -> LinearPlanReviewResult:
        final_results: list[str] = []

        async def on_output(  # noqa: RUF029, RUF100 - run_agent requires an async callback.
            output: ContainerOutput,
        ) -> None:
            if output.type == "result" and output.result:
                final_results.append(output.result)

        try:
            result = await deps.run_agent(
                group,
                group.jid,
                [{"role": "user", "content": _review_prompt(request)}],
                on_output=on_output,
                extra_system_notices=None,
                is_scheduled_task=True,
                repo_access_override=None,
                input_source=(
                    "external:hidden_plan_review" if request.public_source else "hidden_plan_review"
                ),
            )
            if result != "success" or not final_results:
                raise RuntimeError(_REVIEWER_RESULT_ERROR)
            return _parse_result(final_results[-1])
        except Exception as exc:  # noqa: BLE001, RUF100 - reviewer failures become typed admission results.
            logger.exception(
                "Hidden Linear plan review failed",
                issue=request.identifier,
                error_type=type(exc).__name__,
            )
            return LinearPlanReviewResult(
                decision=LinearPlanReviewDecision.ERROR,
                reason=f"{type(exc).__name__}: {_REVIEWER_RESULT_ERROR}",
            )

    return await deps.queue.run_serialized_task(
        RuntimeTarget.from_workspace(group),
        f"linear-plan-review-{hashlib.sha256(request.updated_at.encode()).hexdigest()[:16]}",
        run_review,
    )


async def review_linear_plan(
    deps: PlanReviewDeps,
    request: LinearPlanReviewRequest,
) -> LinearPlanReviewResult:
    """Run one isolated hidden reviewer and return a fail-closed typed result."""
    if not any(profile.folder == request.workspace for profile in deps.workspaces.values()):
        return LinearPlanReviewResult(
            decision=LinearPlanReviewDecision.ERROR,
            reason="Plan reviewer could not resolve the owning workspace",
        )
    return await _run_queued_review(deps, request)
