"""Prompt definitions and named agent pipelines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from pynchy.config.errors import PersonalizationError
from pynchy.config.models import PromptName, ValidatedPromptId, _StrictModel

_PROMPT_SCOPES = frozenset({"souls", "executors", "reviewers", "webhooks"})
_PROMPT_ID_ADAPTER = TypeAdapter(ValidatedPromptId)


def _require_scope(prompt_id: str, scope: str) -> str:
    if not prompt_id.startswith(f"{scope}/"):
        raise ValueError(f"prompt must use the {scope}/ scope")
    return prompt_id


class PromptConfig(_StrictModel):
    """Globally selected Pynchy-owned prompt contexts."""

    default_soul: ValidatedPromptId = PromptName("souls/default")
    default_executor: ValidatedPromptId = PromptName("executors/default")
    default_pipeline: str = "software-delivery"
    cop_inbound: ValidatedPromptId = PromptName("reviewers/cop-inbound")  # noqa: V107
    cop_outbound: ValidatedPromptId = PromptName("reviewers/cop-outbound")  # noqa: V107
    cop_bash: ValidatedPromptId = PromptName("reviewers/cop-bash")  # noqa: V107
    cop_taint: ValidatedPromptId = PromptName("reviewers/cop-taint")  # noqa: V107
    learning: ValidatedPromptId = PromptName("reviewers/learning")
    plan_freshness: ValidatedPromptId = PromptName("reviewers/plan-freshness")  # noqa: V107

    @field_validator("default_soul")
    @classmethod
    def validate_soul(cls, value: str) -> str:
        return _require_scope(value, "souls")

    @field_validator("default_executor")
    @classmethod
    def validate_executor(cls, value: str) -> str:
        return _require_scope(value, "executors")

    @field_validator(
        "cop_inbound",
        "cop_outbound",
        "cop_bash",
        "cop_taint",
        "learning",
        "plan_freshness",
    )
    @classmethod
    def validate_reviewer(cls, value: str) -> str:
        return _require_scope(value, "reviewers")

    @field_validator("default_pipeline")
    @classmethod
    def validate_default_pipeline(cls, value: str) -> str:
        pipeline = value.strip()
        if not pipeline:
            raise ValueError("default pipeline cannot be empty")
        return pipeline


type PipelineStageName = Literal["interactive", "planning", "delivery", "follow-up"]


class PipelineStageConfig(_StrictModel):
    """One ordered executor stage and its independent reviewers."""

    name: PipelineStageName
    executor: ValidatedPromptId
    reviewers: list[ValidatedPromptId] = Field(default_factory=list)

    @field_validator("executor")
    @classmethod
    def validate_executor(cls, value: str) -> str:
        return _require_scope(value, "executors")

    @field_validator("reviewers")
    @classmethod
    def validate_reviewers(cls, value: list[str]) -> list[str]:
        return [_require_scope(prompt_id, "reviewers") for prompt_id in value]


class PipelineConfig(_StrictModel):
    """An ordered reusable agent pipeline."""

    stages: list[PipelineStageConfig]

    @model_validator(mode="after")
    def validate_stages(self) -> PipelineConfig:
        if not self.stages:
            raise ValueError("pipelines require at least one stage")
        names = [stage.name.casefold() for stage in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("pipeline stage names must be unique ignoring case")
        return self

    def stage(self, name: str) -> PipelineStageConfig | None:
        """Return a named stage when this pipeline declares it."""
        return next((stage for stage in self.stages if stage.name == name), None)


class PipelineDocument(BaseModel):
    """One versioned pipeline declaration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    pipeline: PipelineConfig


@dataclass(frozen=True, slots=True)
class PromptCatalog:
    """Validated prompt content keyed by globally unique scoped ID."""

    content: dict[str, str]
    sources: dict[str, Path]

    def read(self, prompt_id: str) -> str:
        try:
            return self.content[prompt_id]
        except KeyError as exc:
            raise PersonalizationError(f"Unknown prompt ID: {prompt_id}") from exc

    def compose(self, prompt_ids: list[str] | tuple[str, ...]) -> str:
        return "\n\n---\n\n".join(self.read(prompt_id) for prompt_id in prompt_ids)


def load_prompt_catalog(
    *,
    default_prompts: Path,
    personalized_prompts: Path,
) -> PromptCatalog:
    """Load distinct prompt definitions from defaults and personalization."""
    content: dict[str, str] = {}
    sources: dict[str, Path] = {}
    for root in (default_prompts, personalized_prompts):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            relative = path.relative_to(root)
            if (
                relative.parts[0] not in _PROMPT_SCOPES
                or (relative.parts[0] != "webhooks" and len(relative.parts) != 2)
                or path.is_symlink()
            ):
                raise PersonalizationError(
                    f"Prompt files must be flat under souls/, executors/, or "
                    f"reviewers/; webhooks/ may be nested: {path}"
                )
            prompt_id = relative.with_suffix("").as_posix()
            try:
                _PROMPT_ID_ADAPTER.validate_python(prompt_id)
            except ValidationError as exc:
                raise PersonalizationError(f"Invalid prompt ID {prompt_id!r}: {exc}") from exc
            if prompt_id in content:
                raise PersonalizationError(
                    f"Duplicate prompt ID {prompt_id!r}: {sources[prompt_id]} and {path}"
                )
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise PersonalizationError(f"Could not read prompt {path}: {exc}") from exc
            if not text:
                raise PersonalizationError(f"Prompt file cannot be blank: {path}")
            content[prompt_id] = text
            sources[prompt_id] = path
    return PromptCatalog(content=content, sources=sources)


def read_prompt(prompt_id: str, project_root: Path) -> str:
    """Resolve one required prompt from the current checkout."""
    root = project_root.resolve()
    return load_prompt_catalog(
        default_prompts=root / "data/defaults/prompts",
        personalized_prompts=root / "data/personalization/prompts",
    ).read(prompt_id)
