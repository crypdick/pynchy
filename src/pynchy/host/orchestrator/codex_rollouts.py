"""Confined discovery and migration of host Codex rollout files."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from pynchy.logger import logger


class CodexRolloutInspectionError(RuntimeError):
    """The host could not determine whether a Codex rollout is durable."""


def rollout_exists(codex_home: Path, thread_id: str) -> bool:
    """Return whether a confined Codex home contains the exact durable rollout."""
    return bool(
        _find_rollouts(
            codex_home / "sessions",
            thread_id,
            confinement_root=codex_home,
        )
    )


def migrate_rollout(
    thread_id: str,
    *,
    codex_home: Path,
    legacy_codex_home: Path,
    scoped_sessions_root: Path,
) -> Path | None:
    """Copy an exact global or sibling rollout into one scoped Codex home."""
    sources: list[tuple[Path, Path]] = []
    legacy_sessions = legacy_codex_home / "sessions"
    sources.extend(
        _find_rollouts(
            legacy_sessions,
            thread_id,
            confinement_root=legacy_codex_home,
        )
    )
    for sibling_sessions in _sibling_session_roots(
        codex_home,
        scoped_sessions_root=scoped_sessions_root,
    ):
        sources.extend(
            _find_rollouts(
                sibling_sessions,
                thread_id,
                confinement_root=scoped_sessions_root,
            )
        )
    if not sources:
        return None

    try:
        source, source_sessions = _select_rollout_source(sources, thread_id)
        destination = _destination_for(
            source,
            source_sessions=source_sessions,
            codex_home=codex_home,
            thread_id=thread_id,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_rollout_atomic(source.resolve(strict=True), destination)
    except CodexRolloutInspectionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CodexRolloutInspectionError(
            f"Could not scope Codex rollout from {source} into {codex_home}"
        ) from exc
    logger.info("Scoped Codex rollout", thread_id=thread_id, destination=str(destination))
    return destination


def _find_rollouts(
    sessions_root: Path,
    thread_id: str,
    *,
    confinement_root: Path,
) -> tuple[tuple[Path, Path], ...]:
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
    rollouts: list[tuple[Path, Path]] = []
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
            rollouts.append((resolved_candidate, resolved_root))
    return tuple(rollouts)


def _select_rollout_source(
    sources: list[tuple[Path, Path]],
    thread_id: str,
) -> tuple[Path, Path]:
    """Choose only among byte-identical sources; reject ambiguous histories."""
    try:
        digests = {_rollout_digest(source) for source, _sessions_root in sources}
    except OSError as exc:
        raise CodexRolloutInspectionError(
            f"Could not compare Codex rollout sources for {thread_id}"
        ) from exc
    if len(digests) != 1:
        locations = ", ".join(str(source) for source, _sessions_root in sources)
        raise CodexRolloutInspectionError(
            f"Divergent Codex rollout sources for {thread_id}: {locations}"
        )

    # Lexical choice is safe only after content identity is established.
    return min(sources, key=lambda source: str(source[0]))


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


def _sibling_session_roots(
    codex_home: Path,
    *,
    scoped_sessions_root: Path,
) -> tuple[Path, ...]:
    """Return scoped sibling homes without crossing the configured sessions root."""
    sessions_root = scoped_sessions_root
    try:
        resolved_root = sessions_root.resolve(strict=True)
    except FileNotFoundError:
        return ()
    except (OSError, RuntimeError) as exc:
        raise CodexRolloutInspectionError(
            f"Could not inspect scoped Codex homes beneath {sessions_root}"
        ) from exc

    resolved_target = (codex_home / "sessions").resolve(strict=False)
    if codex_home.name != ".codex" or not resolved_target.is_relative_to(resolved_root):
        return ()
    siblings: list[Path] = []
    try:
        workspaces = sorted(sessions_root.iterdir())
    except OSError as exc:
        raise CodexRolloutInspectionError(
            f"Could not inspect scoped Codex homes beneath {sessions_root}"
        ) from exc
    for workspace in workspaces:
        candidate = workspace / ".codex" / "sessions"
        try:
            resolved_candidate = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            if candidate.is_symlink():
                raise CodexRolloutInspectionError(
                    f"Broken sibling Codex sessions link: {candidate}"
                ) from exc
            continue
        except (OSError, RuntimeError) as exc:
            raise CodexRolloutInspectionError(
                f"Could not resolve sibling Codex sessions path: {candidate}"
            ) from exc
        try:
            resolved_candidate.relative_to(resolved_root)
        except (RuntimeError, ValueError) as exc:
            raise CodexRolloutInspectionError(
                f"Sibling Codex sessions path escapes its scope: {candidate}"
            ) from exc
        if resolved_candidate != resolved_target:
            siblings.append(candidate)
    return tuple(siblings)


def _destination_for(
    source: Path,
    *,
    source_sessions: Path,
    codex_home: Path,
    thread_id: str,
) -> Path:
    try:
        return _validated_destination_for(
            source,
            source_sessions=source_sessions,
            codex_home=codex_home,
            thread_id=thread_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CodexRolloutInspectionError(
            f"Codex rollout path is outside its scoped sessions root: {source}"
        ) from exc


def _validated_destination_for(
    source: Path,
    *,
    source_sessions: Path,
    codex_home: Path,
    thread_id: str,
) -> Path:
    target_sessions = codex_home / "sessions"
    resolved_source = source.resolve(strict=True)
    resolved_source_root = source_sessions.resolve(strict=True)
    relative_source = source.relative_to(source_sessions)
    resolved_source.relative_to(resolved_source_root)
    destination = target_sessions / relative_source
    _require_destination_confined(destination, codex_home, target_sessions)
    if resolved_source == destination.resolve(strict=False):
        raise ValueError("source and destination are identical")
    if not destination.exists() and not destination.is_symlink():
        return destination

    destination = (
        target_sessions / "recovered" / thread_id / _rollout_digest(resolved_source) / source.name
    )
    _require_destination_confined(destination, codex_home, target_sessions)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Recovered rollout destination exists: {destination}")
    if resolved_source == destination.resolve(strict=False):
        raise ValueError("source and recovered destination are identical")
    return destination


def _require_destination_confined(
    destination: Path,
    codex_home: Path,
    target_sessions: Path,
) -> None:
    resolved_home = codex_home.resolve(strict=False)
    resolved_sessions = target_sessions.resolve(strict=False)
    resolved_sessions.relative_to(resolved_home)
    destination.resolve(strict=False).relative_to(resolved_sessions)


def _rollout_digest(source: Path) -> str:
    with source.open("rb") as rollout:
        return hashlib.file_digest(rollout, "sha256").hexdigest()


def _copy_rollout_atomic(source: Path, destination: Path) -> None:
    """Durably publish a complete rollout without exposing a partial file."""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb") as copied:
            os.fsync(copied.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
