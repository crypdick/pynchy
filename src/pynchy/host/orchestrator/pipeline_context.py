"""Select configured executor and reviewer contexts for one agent turn."""

from __future__ import annotations

from typing import Any, Protocol, cast, runtime_checkable

from pynchy.workspace.api import (
    ResolvedWorkspaceConfig,
)

type Settings = Any


@runtime_checkable
class PipelineStage(Protocol):
    executor: str
    reviewers: list[str]


def prompt_ids_for_context(
    resolved: ResolvedWorkspaceConfig | None,
    input_source: str,
    *,
    settings: Settings,
) -> tuple[str, ...]:
    """Return the selected soul and role prompt for one agent context."""
    prompts = settings.prompts
    soul = resolved.soul if resolved is not None and resolved.soul else prompts.default_soul
    reviewer_prefix = "hidden:pipeline-review:"
    if input_source.startswith(reviewer_prefix):
        reviewer = input_source.removeprefix(reviewer_prefix)
        if not reviewer.startswith("reviewers/"):
            raise ValueError("pipeline reviewer prompt must use the reviewers/ scope")
        return soul, reviewer
    stage = pipeline_stage_for_context(resolved, input_source, settings=settings)
    executor = stage.executor if stage is not None else prompts.default_executor
    return tuple(dict.fromkeys((soul, prompts.default_executor, executor)))


def pipeline_stage_for_context(
    resolved: ResolvedWorkspaceConfig | None,
    input_source: str,
    *,
    settings: Settings,
) -> PipelineStage | None:
    """Return the configured pipeline stage for one executor invocation."""
    pipeline_name = (
        resolved.pipeline
        if resolved is not None and resolved.pipeline
        else settings.prompts.default_pipeline
    )
    pipeline = settings.pipelines.get(pipeline_name)
    if pipeline is None:
        return None
    return cast(
        "PipelineStage | None",
        pipeline.stage(_stage_name_for_input_source(input_source)) or pipeline.stage("interactive"),
    )


def reviewer_ids_for_context(
    resolved: ResolvedWorkspaceConfig | None,
    input_source: str,
    *,
    settings: Settings,
) -> tuple[str, ...]:
    """Return independent reviewers selected for one pipeline stage."""
    stage = pipeline_stage_for_context(resolved, input_source, settings=settings)
    return tuple(stage.reviewers) if stage is not None else ()


def _stage_name_for_input_source(input_source: str) -> str:
    if input_source.endswith(":linear:ready_for_planning"):
        return "planning"
    if input_source.endswith(":linear:authorized"):
        return "delivery"
    if input_source.endswith(":linear:follow-ups"):
        return "follow-up"
    return "interactive"
