"""Confined discovery of durable Codex rollout files."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pynchy.logger import logger


class CodexRolloutInspectionError(RuntimeError):
    """The host could not determine whether a Codex rollout is durable."""


def rollout_exists(codex_home: Path, thread_id: str) -> bool:
    """Return whether a confined Codex home contains the exact durable rollout."""
    return bool(_find_rollouts(codex_home / "sessions", thread_id, confinement_root=codex_home))


def prepare_rollout_resume(codex_home: Path, thread_id: str) -> bool:
    """Verify one exact rollout and repair its stale Codex state path."""
    rollouts = _find_rollouts(codex_home / "sessions", thread_id, confinement_root=codex_home)
    if not rollouts:
        return False
    if len(rollouts) != 1:
        raise CodexRolloutInspectionError(
            f"Found multiple Codex rollouts for thread {thread_id} in {codex_home}"
        )
    _relocate_state_rollout(codex_home, thread_id, rollouts[0])
    return True


def _relocate_state_rollout(codex_home: Path, thread_id: str, rollout: Path) -> None:
    """Point one exact Codex state row at its verified rollout after a host move."""
    database = codex_home / "state_5.sqlite"
    if not database.exists():
        if database.is_symlink():
            raise CodexRolloutInspectionError(
                f"Codex state database is a broken symlink: {database}"
            )
        return
    try:
        resolved_database = database.resolve(strict=True)
        resolved_database.relative_to(codex_home.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise CodexRolloutInspectionError(
            f"Codex state database is outside its home: {database}"
        ) from exc
    if not resolved_database.is_file():
        raise CodexRolloutInspectionError(f"Codex state database is not a file: {database}")

    try:
        with sqlite3.connect(resolved_database) as connection:
            relocated = _relocate_state_row(connection, thread_id, rollout)
    except sqlite3.Error as exc:
        raise CodexRolloutInspectionError(
            f"Could not inspect Codex state for thread {thread_id}"
        ) from exc
    if relocated:
        logger.info("Relocated Codex rollout state", thread_id=thread_id, rollout=str(rollout))


def _relocate_state_row(connection: sqlite3.Connection, thread_id: str, rollout: Path) -> bool:
    row = connection.execute(
        "SELECT rollout_path FROM threads WHERE id = ?",
        (thread_id,),
    ).fetchone()
    if row is None or row[0] == str(rollout):
        return False
    stale_path = Path(row[0])
    if not stale_path.is_absolute() or stale_path.exists():
        raise CodexRolloutInspectionError(
            f"Refusing to replace available Codex rollout path for thread {thread_id}"
        )
    updated = connection.execute(
        "UPDATE threads SET rollout_path = ? WHERE id = ? AND rollout_path = ?",
        (str(rollout), thread_id, row[0]),
    )
    if updated.rowcount != 1:
        raise CodexRolloutInspectionError(
            f"Codex state changed while relocating thread {thread_id}"
        )
    return True


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
