"""Validated runtime configuration loading and change classification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from pynchy.config.personalization import (
    PersonalizationError,
    PersonalizationPaths,
    validate_litellm_model_names,
    validate_personalization_tree,
)
from pynchy.config.settings import Settings

_LIVE_PROFILE_FIELDS = frozenset(
    {
        "skills",
        "denied_skills",
        "repo",
        "model",
        "execution_mode",
        "cwd",
        "contains_secrets",
        "cop_active",
        "capabilities",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimePolicyChanges:
    """Live policy changes after resolving every registered workspace."""

    affected_workspaces: tuple[str, ...]
    live_changed: bool


def load_runtime_candidate() -> Settings:
    """Load and fully validate settings through the normal runtime sources."""
    paths = PersonalizationPaths.for_project(Path.cwd())
    validate_personalization_tree(paths.project_root, paths.personalization)
    candidate = Settings()
    if candidate.gateway.litellm_config is None:
        raise PersonalizationError("Runtime settings must select a LiteLLM configuration")
    validate_litellm_model_names(
        Path(candidate.gateway.litellm_config),
        candidate.configured_agent_models(),
    )
    return candidate


def restart_fingerprint(settings: Settings) -> str:
    """Hash settings that still require a process restart."""
    payload = settings.model_dump(mode="python")
    for profile in payload["profiles"].values():
        for field in _LIVE_PROFILE_FIELDS:
            profile.pop(field, None)
    for workspace in payload["workspaces"].values():
        workspace.pop("soul", None)
        workspace.pop("pipeline", None)
        workspace.pop("model", None)
        workspace.pop("model_reasoning_effort", None)
        for thread in workspace["threads"]:
            thread.pop("model", None)
            thread.pop("model_reasoning_effort", None)
        for scope in workspace["scopes"]:
            scope.pop("model", None)
            scope.pop("model_reasoning_effort", None)
    payload["agent"].pop("model_reasoning_effort", None)
    payload.pop("prompts", None)
    payload.pop("pipelines", None)
    for field in ("image", "timeout_ms", "memory_mb", "idle_timeout_ms"):
        payload["container"].pop(field, None)
    for field in ("enabled", "review_after_turn", "max_attempts", "packet_max_chars"):
        payload["learning"].pop(field, None)
    payload["learning"].pop("obsidian", None)
    payload["security"].pop("blocked_patterns", None)
    for name in _automation_names(settings.project_root):
        payload["jobs"].pop(name, None)
    payload["__restart_sources__"] = _restart_source_projection(settings.project_root)
    encoded = json.dumps(
        _fingerprint_value(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def skill_policy_projection(
    settings: Settings,
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    """Return the live-refreshable profile skill policy."""
    return tuple(
        (name, tuple(profile.skills), tuple(profile.denied_skills))
        for name, profile in sorted(settings.profiles.items())
    )


def runtime_policy_changes(
    published: Settings,
    candidate: Settings,
    workspace_folders: tuple[str, ...],
) -> RuntimePolicyChanges:
    """Classify live drift and the registered runtimes that need replacement."""
    global_retirement = _global_retirement_projection(published) != _global_retirement_projection(
        candidate
    )
    affected = tuple(
        sorted(
            workspace_folders
            if global_retirement
            else (
                folder
                for folder in workspace_folders
                if _workspace_retirement_projection(published, folder)
                != _workspace_retirement_projection(candidate, folder)
            )
        )
    )
    live_changed = bool(affected) or global_retirement
    live_changed = live_changed or (
        skill_policy_projection(published) != skill_policy_projection(candidate)
    )
    live_changed = live_changed or (
        _next_turn_projection(published) != _next_turn_projection(candidate)
    )
    return RuntimePolicyChanges(affected, live_changed)


def automation_projection(settings: Settings) -> tuple[tuple[str, object], ...]:
    """Return the live-refreshable file-backed automation policy."""
    projection = []
    for name in sorted(_automation_names(settings.project_root)):
        job = settings.jobs.get(name)
        if job is None:
            continue
        value = job.model_dump(mode="python")
        projection.append((name, _fingerprint_value(value)))
    return tuple(projection)


def _workspace_retirement_projection(
    settings: Settings,
    workspace_folder: str,
) -> object:
    resolved = settings.resolved_workspace_config(workspace_folder)
    if resolved is None:
        return None
    return (
        resolved.soul,
        resolved.pipeline,
        tuple(resolved.repo),
        resolved.model,
        resolved.model_reasoning_effort,
        resolved.execution_mode,
        resolved.cwd,
        resolved.contains_secrets,
        resolved.cop_active,
        tuple((name, rule.decision) for name, rule in sorted(resolved.capabilities.items())),
    )


def _global_retirement_projection(settings: Settings) -> object:
    learning = settings.learning
    return (
        settings.agent.model_reasoning_effort,
        learning.enabled,
        learning.obsidian.vault_root,
        learning.obsidian.mount_path,
        learning.obsidian.default_profile_root,
        learning.obsidian.memory_dir_name,
        tuple(settings.security.blocked_patterns),
        settings.container.image,
        settings.container.memory_mb,
        settings.container.idle_timeout_ms,
        _fingerprint_value(settings.prompts.model_dump()),
        _fingerprint_value(
            {name: pipeline.model_dump() for name, pipeline in sorted(settings.pipelines.items())}
        ),
        _prompt_source_projection(settings.project_root),
    )


def _next_turn_projection(settings: Settings) -> object:
    return (
        settings.learning.review_after_turn,
        settings.learning.max_attempts,
        settings.learning.packet_max_chars,
        settings.container.timeout_ms,
    )


def configuration_source_digest(project_root: Path) -> str:
    """Hash all files read while loading or validating runtime configuration."""
    root = project_root.resolve()
    digest = hashlib.sha256()
    digest.update(_source_bytes(root, root / ".env"))
    for source_root in (
        root / "data" / "defaults",
        root / "data" / "personalization",
    ):
        relative_root = source_root.relative_to(root).as_posix()
        if not source_root.is_dir():
            digest.update(f"{relative_root}\0__missing__\0".encode())
            continue
        for path in sorted(source_root.rglob("*")):
            relative = path.relative_to(root)
            if ".git" in relative.parts or (not path.is_file() and not path.is_symlink()):
                continue
            digest.update(_source_bytes(root, path))
    return digest.hexdigest()


def _source_bytes(root: Path, path: Path) -> bytes:
    relative = path.relative_to(root).as_posix()
    try:
        if path.is_symlink():
            content = b"__symlink__:" + str(path.readlink()).encode()
        else:
            content = path.read_bytes() if path.is_file() else b"__missing__"
    except FileNotFoundError:
        content = b"__changed_during_read__"
    return relative.encode() + b"\0" + content + b"\0"


def _restart_source_projection(project_root: Path) -> dict[str, object]:
    sources: dict[str, object] = {}
    for path in (
        project_root / ".env",
        project_root / "data/personalization/litellm.yaml",
    ):
        sources[path.relative_to(project_root).as_posix()] = _restart_file_digest(path)
    return sources


def _automation_names(project_root: Path) -> set[str]:
    names: set[str] = set()
    for directory in (
        project_root / "data/defaults/automations",
        project_root / "data/personalization/automations",
    ):
        if directory.is_dir():
            names.update(path.parent.name for path in directory.glob("*/config.toml"))
    return names


def _prompt_source_projection(project_root: Path) -> tuple[tuple[str, str], ...]:
    sources: list[tuple[str, str]] = []
    for directory in (
        project_root / "data/defaults/prompts",
        project_root / "data/personalization/prompts",
    ):
        if not directory.is_dir():
            continue
        sources.extend(
            (
                path.relative_to(project_root).as_posix(),
                _restart_file_digest(path),
            )
            for path in sorted(directory.rglob("*.md"))
            if path.is_file()
        )
    return tuple(sources)


def _restart_file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "__missing__"


def _fingerprint_value(value: object) -> object:
    if isinstance(value, SecretStr):
        result: object = value.get_secret_value()
    elif isinstance(value, Path):
        result = str(value)
    elif isinstance(value, Mapping):
        result = {str(key): _fingerprint_value(child) for key, child in value.items()}
    elif isinstance(value, (list, tuple)):
        result = [_fingerprint_value(child) for child in value]
    else:
        result = value
    return result
