"""Confined discovery of durable Codex rollout files."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 - beartype resolves rollout path annotations at runtime.

from pynchy.logger import logger


class CodexRolloutInspectionError(RuntimeError):
    """The host could not determine whether a Codex rollout is durable."""


def rollout_exists(codex_home: Path, thread_id: str) -> bool:
    """Return whether a confined Codex home contains the exact durable rollout."""
    return bool(_find_rollouts(codex_home / "sessions", thread_id, confinement_root=codex_home))


def _find_rollouts(
    sessions_root: Path,
    thread_id: str,
    *,
    confinement_root: Path,
) -> tuple[Path, ...]:
    """Find exact rollouts without following a candidate outside its scope."""
    expected_suffix = f"-{thread_id}.jsonl"
    try:
        resolved_root = sessions_root.resolve(strict=True)
    except FileNotFoundError as exc:
        if sessions_root.is_symlink():
            raise CodexRolloutInspectionError(
                f"Codex sessions root is a broken symlink: {sessions_root}"
            ) from exc
        return ()
    except (OSError, RuntimeError) as exc:
        raise CodexRolloutInspectionError(
            f"Could not inspect Codex sessions root {sessions_root}"
        ) from exc

    try:
        resolved_root.relative_to(confinement_root.resolve(strict=False))
        candidates = sorted(sessions_root.rglob("*.jsonl"))
    except (OSError, RuntimeError, ValueError) as exc:
        raise CodexRolloutInspectionError(
            f"Could not inspect confined Codex rollouts at {sessions_root}"
        ) from exc
    if not resolved_root.is_dir():
        raise CodexRolloutInspectionError(
            f"Codex sessions root is not a directory: {sessions_root}"
        )
    rollouts: list[Path] = []
    for candidate in candidates:
        if not (candidate.name.startswith("rollout-") and candidate.name.endswith(expected_suffix)):
            continue
        try:
            resolved_candidate = candidate.resolve(strict=True)
            resolved_candidate.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CodexRolloutInspectionError(
                f"Codex rollout path is outside its sessions root: {candidate}"
            ) from exc
        try:
            header_matches = _has_session_header(resolved_candidate, thread_id)
        except OSError as exc:
            raise CodexRolloutInspectionError(
                f"Could not inspect Codex rollout header at {candidate}"
            ) from exc
        if header_matches:
            rollouts.append(resolved_candidate)
    return tuple(rollouts)


def _has_session_header(path: Path, thread_id: str) -> bool:
    """Return whether the first durable record identifies this exact thread."""
    try:
        with path.open(encoding="utf-8") as rollout:
            first_record = next((line for line in rollout if line.strip()), None)
    except UnicodeDecodeError:
        logger.warning("Ignoring non-UTF-8 Codex rollout header", path=str(path))
        return False
    if first_record is None:
        logger.warning("Ignoring empty Codex rollout", path=str(path))
        return False
    try:
        header = json.loads(first_record)
    except json.JSONDecodeError:
        logger.warning("Ignoring corrupt Codex rollout header", path=str(path))
        return False
    return (
        isinstance(header, dict)
        and header.get("type") == "session_meta"
        and isinstance(header.get("payload"), dict)
        and header["payload"].get("id") == thread_id
    )
