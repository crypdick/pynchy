"""Hidden agent review of approved Linear plans before execution admission."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pynchy.agent_protocol.api import (
    ContainerOutput,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pynchy.host.orchestrator.concurrency import GroupQueue

from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspacePolicy,
    register_runtime_workspace_policy,
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
_REVIEWER_REASONING_EFFORT = "medium"


@runtime_checkable
class PlanReviewDeps(Protocol):
    """Host capabilities needed by one hidden plan review."""

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def run_agent(  # noqa: PLR0913 - mirrors the orchestrator contract.
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
        model_reasoning_effort_override: str | None = None,
    ) -> str: ...


def _reviewer_profile(request: LinearPlanReviewRequest) -> WorkspaceProfile:
    digest = hashlib.sha256(request.issue_id.encode()).hexdigest()[:16]
    folder = f"linear-plan-review-{digest}"
    register_runtime_workspace_policy(
        folder,
        RuntimeWorkspacePolicy(
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


def _review_prompt(request: LinearPlanReviewRequest, reviewer_prompt: str) -> str:
    issue = {
        "id": request.issue_id,
        "identifier": request.identifier,
        "title": request.title,
        "url": request.url,
        "updated_at": request.updated_at,
        "description": request.description,
    }
    return (
        f"{reviewer_prompt}\n\n"
        f"Current Linear issue:\n{json.dumps(issue, ensure_ascii=False, indent=2)}"
    )


def _parse_result(raw: str) -> LinearPlanReviewResult:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise TypeError("reviewer output must be a JSON object")
    raw_decision = payload.get("decision")
    reason = payload.get("reason")
    plan = payload.get("plan")
    if not isinstance(raw_decision, str) or not isinstance(reason, str):
        raise TypeError("reviewer decision and reason must be strings")
    if plan is not None and not isinstance(plan, str):
        raise TypeError("reviewer plan must be a string when present")
    return LinearPlanReviewResult(
        decision=LinearPlanReviewDecision(raw_decision),
        reason=reason,
        plan=plan,
    )


async def _run_queued_review(
    deps: PlanReviewDeps,
    request: LinearPlanReviewRequest,
    reviewer_prompt: str,
) -> LinearPlanReviewResult:
    group = _reviewer_profile(request)

    async def run_review() -> LinearPlanReviewResult:
        async def run_turn(prompt: str) -> str:
            final_results: list[str] = []
            runner_errors: list[str] = []

            async def on_output(  # noqa: RUF029 - run_agent requires an async callback.
                output: ContainerOutput,
            ) -> None:
                if output.error:
                    runner_errors.append(output.error)
                if output.type == "result" and output.result:
                    final_results.append(output.result)

            result = await deps.run_agent(
                group,
                group.jid,
                [{"role": "user", "content": prompt}],
                on_output=on_output,
                extra_system_notices=None,
                is_scheduled_task=True,
                repo_access_override=None,
                input_source=(
                    "external:hidden_plan_review" if request.public_source else "hidden_plan_review"
                ),
                model_reasoning_effort_override=_REVIEWER_REASONING_EFFORT,
            )
            if result != "success":
                detail = runner_errors[-1] if runner_errors else f"agent returned {result}"
                raise RuntimeError(detail)
            if not final_results:
                detail = runner_errors[-1] if runner_errors else "reviewer returned no final result"
                raise RuntimeError(detail)
            return final_results[-1]

        try:
            raw = await run_turn(_review_prompt(request, reviewer_prompt))
            try:
                return _parse_result(raw)
            except (TypeError, ValueError) as exc:
                repair_prompt = (
                    "Your previous response was invalid. Correct it and return exactly one "
                    "JSON object with decision, reason, and plan when required.\n\n"
                    f"Validation error: {type(exc).__name__}: {exc}\n\n"
                    f"Previous response:\n{raw}"
                )
                return _parse_result(await run_turn(repair_prompt))
        except Exception as exc:  # noqa: BLE001 - reviewer failures become typed admission results.
            logger.exception(
                "Hidden Linear plan review failed",
                issue=request.identifier,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return LinearPlanReviewResult(
                decision=LinearPlanReviewDecision.ERROR,
                reason=f"{type(exc).__name__}: {str(exc) or _REVIEWER_RESULT_ERROR}",
            )

    return await deps.queue.run_serialized_task(
        RuntimeTarget.from_workspace(group),
        f"linear-plan-review-{hashlib.sha256(request.updated_at.encode()).hexdigest()[:16]}",
        run_review,
    )


async def review_linear_plan(
    deps: PlanReviewDeps,
    request: LinearPlanReviewRequest,
    reviewer_prompt: str,
) -> LinearPlanReviewResult:
    """Run one isolated hidden reviewer and return a fail-closed typed result."""
    if not any(profile.folder == request.workspace for profile in deps.workspaces.values()):
        return LinearPlanReviewResult(
            decision=LinearPlanReviewDecision.ERROR,
            reason="Plan reviewer could not resolve the owning workspace",
        )
    return await _run_queued_review(deps, request, reviewer_prompt)
