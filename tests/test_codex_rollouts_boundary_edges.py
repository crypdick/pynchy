"""Boundary behavior for confined Codex rollout discovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pynchy.host.orchestrator import codex_rollouts

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

THREAD_ID = "019f6106-fd23-7292-bac5-7dbb7da29002"


class _CasMissConnection(sqlite3.Connection):
    def execute(self, statement: str, parameters: Iterable[Any] = (), /) -> sqlite3.Cursor:
        if statement.startswith("UPDATE threads SET rollout_path"):
            return super().execute("UPDATE threads SET rollout_path = rollout_path WHERE 0")
        return super().execute(statement, parameters)


def _write_rollout(home: Path, *, thread_id: str = THREAD_ID, body: str = "") -> Path:
    directory = home / "sessions" / "2026" / "07" / "30"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-07-30T07-27-55-{thread_id}.jsonl"
    path.write_text(
        '{"type":"session_meta","payload":{"id":"' + thread_id + '"}}\n' + body,
        encoding="utf-8",
    )
    return path


def _write_state(codex_home: Path, rollout_path: Path) -> Path:
    database = codex_home / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
        connection.execute("INSERT INTO threads VALUES (?, ?)", (THREAD_ID, str(rollout_path)))
    return database


def test_rollout_exists_rejects_broken_sessions_link(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "sessions").symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="broken symlink"):
        codex_rollouts.rollout_exists(codex_home, THREAD_ID)


def test_rollout_exists_rejects_sessions_root_that_is_not_a_directory(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "sessions").write_text("not a directory", encoding="utf-8")

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="not a directory"):
        codex_rollouts.rollout_exists(codex_home, THREAD_ID)


def test_rollout_exists_ignores_non_rollout_jsonl(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions" / "2026"
    sessions.mkdir(parents=True)
    (sessions / "other.jsonl").write_text("{}", encoding="utf-8")

    assert codex_rollouts.rollout_exists(tmp_path / ".codex", THREAD_ID) is False


def test_rollout_exists_rejects_candidate_escaping_sessions_root(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026"
    sessions.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}", encoding="utf-8")
    (sessions / f"rollout-2026-07-30T07-27-55-{THREAD_ID}.jsonl").symlink_to(outside)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="outside"):
        codex_rollouts.rollout_exists(codex_home, THREAD_ID)


def test_rollout_exists_rejects_unreadable_candidate_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollout = _write_rollout(tmp_path / ".codex")
    original_open = Path.open

    def fail_open(path: Path, *args, **kwargs):
        if path == rollout:
            raise OSError("rollout storage unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_open)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="header"):
        codex_rollouts.rollout_exists(tmp_path / ".codex", THREAD_ID)


def test_rollout_exists_propagates_sessions_root_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    original_resolve = Path.resolve

    def fail_root(path: Path, *args, **kwargs):
        if path == sessions:
            raise OSError("root unavailable")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_root)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="sessions root"):
        codex_rollouts.rollout_exists(codex_home, THREAD_ID)


def test_prepare_resume_rejects_state_database_outside_codex_home(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(codex_home)
    outside = tmp_path / "state_5.sqlite"
    with sqlite3.connect(outside) as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
    (codex_home / "state_5.sqlite").symlink_to(outside)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="outside"):
        codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID)


def test_prepare_resume_refuses_to_replace_available_stored_rollout(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(codex_home)
    stored_rollout = tmp_path / "still-available.jsonl"
    stored_rollout.write_text("available", encoding="utf-8")
    database = _write_state(codex_home, stored_rollout)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="Refusing"):
        codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT rollout_path FROM threads WHERE id = ?", (THREAD_ID,)
        ).fetchone() == (str(stored_rollout),)


def test_prepare_resume_cas_miss_fails_without_changing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(codex_home)
    stale_rollout = Path("/missing/old-rollout.jsonl")
    database = _write_state(codex_home, stale_rollout)
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        codex_rollouts.sqlite3,
        "connect",
        lambda path: real_connect(path, factory=_CasMissConnection),
    )

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="state changed"):
        codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID)

    with real_connect(database) as verifier:
        assert verifier.execute(
            "SELECT rollout_path FROM threads WHERE id = ?", (THREAD_ID,)
        ).fetchone() == (str(stale_rollout),)
