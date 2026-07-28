"""Validated runtime configuration loading and change classification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from pathlib import Path

from pydantic import SecretStr

from pynchy.config.personalization import (
    PersonalizationError,
    PersonalizationPaths,
    validate_litellm_model_names,
    validate_personalization_tree,
)
from pynchy.config.settings import Settings


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
    """Hash every setting except profile skill grants and denials."""
    payload = settings.model_dump(mode="python")
    for profile in payload["profiles"].values():
        profile.pop("skills", None)
        profile.pop("denied_skills", None)
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
    for directory in (
        project_root / "data/defaults/automations",
        project_root / "data/personalization/automations",
    ):
        relative = directory.relative_to(project_root).as_posix()
        sources[relative] = (
            {
                path.relative_to(directory).as_posix(): _restart_file_digest(path)
                for path in sorted(directory.rglob("*"))
                if path.is_file()
            }
            if directory.is_dir()
            else "__missing__"
        )
    return sources


def _restart_file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "__missing__"


def _fingerprint_value(value: object) -> object:
    if isinstance(value, SecretStr):
        result: object = value.get_secret_value()
    elif isinstance(value, Enum):
        result = value.value
    elif isinstance(value, Path):
        result = str(value)
    elif isinstance(value, Mapping):
        result = {str(key): _fingerprint_value(child) for key, child in value.items()}
    elif isinstance(value, (list, tuple)):
        result = [_fingerprint_value(child) for child in value]
    elif isinstance(value, (set, frozenset)):
        result = sorted((_fingerprint_value(child) for child in value), key=repr)
    else:
        result = value
    return result
