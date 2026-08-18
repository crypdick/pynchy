"""Layered defaults and deployment personalization loading."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from pynchy.config.automations import load_automations
from pynchy.config.errors import PersonalizationError
from pynchy.config.models import (
    WorkspaceConfig,  # noqa: TC001 - Pydantic resolves the field annotation at runtime.
)
from pynchy.config.prompts import (
    PipelineConfig,
    PipelineDocument,
    PromptConfig,
    load_prompt_catalog,
)
from pynchy.host.paths import PERSONALIZATION_RELATIVE_DIR, SKILLS_DIRNAME

DEFAULTS_RELATIVE_DIR = Path("data/defaults")
SETTINGS_FILENAME = "pynchy.toml"
LITELLM_FILENAME = "litellm.yaml"
AUTOMATIONS_DIRNAME = "automations"
PIPELINES_DIRNAME = "pipelines"
PROMPTS_DIRNAME = "prompts"
WORKSPACES_DIRNAME = "workspaces"
_INLINE_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "client_secret",
        "master_key",
        "password",
        "refresh_token",
        "secret_access_key",
    }
)


class WorkspaceDocument(BaseModel):
    """One versioned workspace declaration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    workspace: WorkspaceConfig


@dataclass(frozen=True, slots=True)
class PersonalizationPaths:
    """Canonical paths for a Pynchy checkout's configuration layers."""

    project_root: Path
    defaults: Path
    personalization: Path

    @classmethod
    def for_project(
        cls,
        project_root: Path,
        *,
        personalization_root: Path | None = None,
    ) -> PersonalizationPaths:
        root = project_root.resolve()
        personalization = (
            personalization_root.resolve()
            if personalization_root is not None
            else root / PERSONALIZATION_RELATIVE_DIR
        )
        return cls(
            project_root=root,
            defaults=root / DEFAULTS_RELATIVE_DIR,
            personalization=personalization,
        )

    @property
    def litellm_config(self) -> Path:
        return self.personalization / LITELLM_FILENAME

    @property
    def default_prompts(self) -> Path:
        """Return the public baseline prompt directory."""
        return self.defaults / PROMPTS_DIRNAME

    @property
    def personalized_prompts(self) -> Path:
        """Return the deployment-owned prompt directory."""
        return self.personalization / PROMPTS_DIRNAME

    @property
    def personalized_skills(self) -> Path:
        """Return the deployment-owned skill directory."""
        return self.personalization / SKILLS_DIRNAME


def load_layered_settings_mapping(
    project_root: Path,
    *,
    personalization_root: Path | None = None,
    require_personalization: bool = False,
    personalization_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load defaults, personalization, and automation files into one mapping."""
    paths = PersonalizationPaths.for_project(
        project_root,
        personalization_root=personalization_root,
    )
    _require_defaults(paths.defaults)
    if require_personalization:
        _require_personalization(paths.personalization)

    merged = _read_settings_file(paths.defaults / SETTINGS_FILENAME, required=True)
    if paths.personalization.is_dir() or personalization_settings is not None:
        personalized = (
            dict(personalization_settings)
            if personalization_settings is not None
            else _read_settings_file(
                paths.personalization / SETTINGS_FILENAME,
                required=require_personalization,
            )
        )
        merged = _deep_merge(merged, personalized)

    automations = _load_automation_layers(paths)
    if automations:
        configured_jobs = merged.get("jobs", {})
        if not isinstance(configured_jobs, dict):
            raise PersonalizationError("The top-level jobs setting must be a mapping")
        collisions = sorted(set(configured_jobs) & set(automations))
        if collisions:
            joined = ", ".join(collisions)
            raise PersonalizationError(
                f"Automation filenames collide with [jobs] entries: {joined}"
            )
        merged["jobs"] = {**configured_jobs, **automations}

    workspaces = _load_workspace_layers(paths)
    if workspaces:
        configured_workspaces = merged.get("workspaces", {})
        if not isinstance(configured_workspaces, dict):
            raise PersonalizationError("The top-level workspaces setting must be a mapping")
        _reject_name_collisions(
            "Workspace filenames",
            configured_workspaces,
            workspaces,
        )
        merged["workspaces"] = {**configured_workspaces, **workspaces}

    pipelines = _load_pipeline_layers(paths)
    if pipelines:
        configured_pipelines = merged.get("pipelines", {})
        if not isinstance(configured_pipelines, dict):
            raise PersonalizationError("The top-level pipelines setting must be a mapping")
        _reject_name_collisions(
            "Pipeline filenames",
            configured_pipelines,
            pipelines,
        )
        merged["pipelines"] = {**configured_pipelines, **pipelines}

    _validate_prompt_configuration(merged, paths)

    if paths.personalization.is_dir():
        _wire_litellm_config(merged, paths.litellm_config, required=require_personalization)
    return merged


def validate_personalization_tree(
    project_root: Path,
    personalization_root: Path,
) -> dict[str, Any]:
    """Validate a complete personalization repository and return its settings mapping."""
    mapping = load_layered_settings_mapping(
        project_root,
        personalization_root=personalization_root,
        require_personalization=True,
    )
    _validate_no_inline_secrets(mapping, source=personalization_root.resolve() / SETTINGS_FILENAME)
    litellm_path = personalization_root.resolve() / LITELLM_FILENAME
    litellm = _validate_litellm_yaml(litellm_path)
    _validate_no_inline_secrets(litellm, source=litellm_path)
    _validate_skills(project_root.resolve(), personalization_root.resolve())
    return mapping


def validate_litellm_model_names(path: Path, required_models: tuple[str, ...]) -> None:
    """Require every configured agent route to exist in LiteLLM's model list."""
    parsed = _validate_litellm_yaml(path)
    model_names = {
        route["model_name"]
        for route in parsed["model_list"]
        if isinstance(route, dict) and isinstance(route.get("model_name"), str)
    }
    missing = sorted(
        model
        for model in required_models
        if not any(
            configured == model or (configured.endswith("/*") and model.startswith(configured[:-1]))
            for configured in model_names
        )
    )
    if missing:
        raise PersonalizationError(
            "Configured agent model routes are missing from LiteLLM model_list: "
            + ", ".join(missing)
        )


def _require_defaults(defaults_root: Path) -> None:
    if not defaults_root.is_dir():
        raise PersonalizationError(f"Bundled defaults directory is missing: {defaults_root}")


def _require_personalization(personalization_root: Path) -> None:
    if not personalization_root.is_dir():
        raise PersonalizationError(
            f"Personalization repository is missing. Check it out at {personalization_root}"
        )


def _read_settings_file(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise PersonalizationError(f"Required settings file is missing: {path}")
        return {}
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PersonalizationError(f"Could not parse settings file {path}: {exc}") from exc
    return dict(parsed)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings while replacing scalar and list values."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            if "type" in existing and "type" in value and existing["type"] != value["type"]:
                merged[key] = dict(value)
            else:
                merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _load_automation_layers(paths: PersonalizationPaths) -> dict[str, dict[str, Any]]:
    defaults = load_automations(paths.defaults / AUTOMATIONS_DIRNAME)
    personalized = load_automations(paths.personalization / AUTOMATIONS_DIRNAME)
    return {**defaults, **personalized}


def _load_workspace_layers(paths: PersonalizationPaths) -> dict[str, dict[str, Any]]:
    defaults = _load_workspaces(paths.defaults / WORKSPACES_DIRNAME)
    personalized = _load_workspaces(paths.personalization / WORKSPACES_DIRNAME)
    _reject_name_collisions("Workspace files", defaults, personalized)
    return {**defaults, **personalized}


def _load_workspaces(directory: Path) -> dict[str, dict[str, Any]]:
    if not directory.is_dir():
        return {}
    workspaces: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.toml")):
        name = _document_name(path, "workspace")
        try:
            document = WorkspaceDocument.model_validate(
                tomllib.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise PersonalizationError(f"Invalid workspace {path}: {exc}") from exc
        workspaces[name] = document.workspace.model_dump(exclude_none=True)
    return workspaces


def _load_pipeline_layers(paths: PersonalizationPaths) -> dict[str, dict[str, Any]]:
    defaults = _load_pipelines(paths.defaults / PIPELINES_DIRNAME)
    personalized = _load_pipelines(paths.personalization / PIPELINES_DIRNAME)
    _reject_name_collisions("Pipeline files", defaults, personalized)
    return {**defaults, **personalized}


def _load_pipelines(directory: Path) -> dict[str, dict[str, Any]]:
    if not directory.is_dir():
        return {}
    pipelines: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.toml")):
        name = _document_name(path, "pipeline")
        try:
            document = PipelineDocument.model_validate(
                tomllib.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise PersonalizationError(f"Invalid pipeline {path}: {exc}") from exc
        pipelines[name] = document.pipeline.model_dump()
    return pipelines


def _document_name(path: Path, kind: str) -> str:
    name = path.stem
    if not name or name.startswith("."):
        raise PersonalizationError(f"Invalid {kind} filename: {path.name}")
    return name


def _reject_name_collisions(
    label: str,
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> None:
    collisions = sorted(set(left) & set(right))
    if collisions:
        raise PersonalizationError(f"{label} must be globally unique: {', '.join(collisions)}")


def _validate_prompt_configuration(
    settings: Mapping[str, Any],
    paths: PersonalizationPaths,
) -> None:
    catalog = load_prompt_catalog(
        default_prompts=paths.default_prompts,
        personalized_prompts=paths.personalized_prompts,
    )
    if not catalog.content and "prompts" not in settings and "pipelines" not in settings:
        return
    try:
        prompts = PromptConfig.model_validate(settings.get("prompts", {}))
        pipelines = {
            name: PipelineConfig.model_validate(value)
            for name, value in dict(settings.get("pipelines", {})).items()
        }
    except (TypeError, ValueError) as exc:
        raise PersonalizationError(f"Invalid prompt configuration: {exc}") from exc

    required = {
        value for field, value in prompts.model_dump().items() if field != "default_pipeline"
    }
    for pipeline in pipelines.values():
        for stage in pipeline.stages:
            required.add(stage.executor)
            required.update(stage.reviewers)

    workspaces = settings.get("workspaces", {})
    for workspace in workspaces.values():
        if isinstance(workspace, Mapping) and isinstance(workspace.get("soul"), str):
            required.add(workspace["soul"])

    missing = sorted(required - set(catalog.content))
    if missing:
        raise PersonalizationError(f"Required prompt IDs do not resolve: {', '.join(missing)}")

    selected_pipelines = {prompts.default_pipeline}
    selected_pipelines.update(
        workspace["pipeline"]
        for workspace in workspaces.values()
        if isinstance(workspace, Mapping) and isinstance(workspace.get("pipeline"), str)
    )
    unknown_pipelines = sorted(selected_pipelines - set(pipelines))
    if unknown_pipelines:
        raise PersonalizationError(
            f"Selected pipeline names do not resolve: {', '.join(unknown_pipelines)}"
        )


def _wire_litellm_config(
    settings: dict[str, Any],
    path: Path,
    *,
    required: bool,
) -> None:
    if required and not path.is_file():
        raise PersonalizationError(f"Required LiteLLM configuration is missing: {path}")
    if not path.is_file():
        return
    gateway = settings.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        raise PersonalizationError("The top-level gateway setting must be a mapping")
    configured = gateway.get("litellm_config")
    if configured is not None and Path(str(configured)).resolve() != path.resolve():
        raise PersonalizationError(
            "gateway.litellm_config is convention-owned; remove it from pynchy.toml"
        )
    gateway["litellm_config"] = str(path.resolve())


def _validate_litellm_yaml(path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PersonalizationError(f"Could not parse LiteLLM configuration {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PersonalizationError(f"LiteLLM configuration must be a mapping: {path}")
    model_list = parsed.get("model_list")
    if not isinstance(model_list, list) or not model_list:
        raise PersonalizationError(
            f"LiteLLM configuration must declare a non-empty model_list: {path}"
        )
    for index, route in enumerate(model_list):
        if not isinstance(route, dict):
            raise PersonalizationError(f"LiteLLM model_list[{index}] must be a mapping")
        if not isinstance(route.get("model_name"), str) or not route["model_name"].strip():
            raise PersonalizationError(
                f"LiteLLM model_list[{index}].model_name must be a non-empty string"
            )
        if not isinstance(route.get("litellm_params"), dict):
            raise PersonalizationError(
                f"LiteLLM model_list[{index}].litellm_params must be a mapping"
            )
    return parsed


def _validate_no_inline_secrets(value: object, *, source: Path, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (
                _is_inline_secret_key(str(key))
                and isinstance(child, str)
                and child
                and not child.startswith("os.environ/")
            ):
                raise PersonalizationError(
                    f"{source}: {child_path} must reference os.environ/VARNAME; "
                    "store its value in Pynchy's root .env"
                )
            _validate_no_inline_secrets(child, source=source, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_no_inline_secrets(
                child,
                source=source,
                path=f"{path}[{index}]",
            )


def _is_inline_secret_key(key: str) -> bool:
    normalized = key.lower()
    if normalized.endswith("_env"):
        return False
    return normalized in _INLINE_SECRET_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )


def _validate_skills(project_root: Path, personalization_root: Path) -> None:
    defaults_root = project_root / DEFAULTS_RELATIVE_DIR / SKILLS_DIRNAME
    personalized_root = personalization_root / SKILLS_DIRNAME
    _skill_names(defaults_root)
    _skill_names(personalized_root)


def _skill_names(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    names: set[str] = set()
    for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        if skill_dir.is_symlink() or any(path.is_symlink() for path in skill_dir.rglob("*")):
            raise PersonalizationError(f"Skill trees cannot contain symlinks: {skill_dir}")
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            raise PersonalizationError(f"Skill is missing SKILL.md: {skill_dir}")
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise PersonalizationError(f"Could not read skill {skill_file}: {exc}") from exc
        parts = text.split("---", 2)
        if len(parts) < 3 or parts[0].strip():
            raise PersonalizationError(f"Skill has invalid YAML frontmatter: {skill_file}")
        try:
            frontmatter = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            raise PersonalizationError(
                f"Skill has invalid YAML frontmatter {skill_file}: {exc}"
            ) from exc
        if not isinstance(frontmatter, dict):
            raise PersonalizationError(f"Skill frontmatter must be a mapping: {skill_file}")
        name = frontmatter.get("name")
        description = frontmatter.get("description")
        tier = frontmatter.get("tier")
        if name != skill_dir.name:
            raise PersonalizationError(
                f"Skill name must match its directory ({skill_dir.name}): {skill_file}"
            )
        if not isinstance(description, str) or not description.strip():
            raise PersonalizationError(f"Skill description must be non-empty: {skill_file}")
        if tier is not None and (not isinstance(tier, str) or not tier.strip()):
            raise PersonalizationError(f"Skill tier must be non-empty when present: {skill_file}")
        names.add(name)
    return names
