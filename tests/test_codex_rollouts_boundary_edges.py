"""Boundary behavior for confined Codex rollout discovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from pynchy.host.orchestrator import codex_rollouts

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from typing import Any

THREAD_ID = "019f6106-fd23-7292-bac5-7dbb7da29002"


class _InterleavingCursor(sqlite3.Cursor):
    def fetchone(self) -> Any | None:
        row = super().fetchone()
        connection = cast("_InterleavingConnection", self.connection)
        if connection.interleave is not None:
            interleave, connection.interleave = connection.interleave, None
            interleave()
        return row


class _InterleavingConnection(sqlite3.Connection):
    interleave: Callable[[], None] | None = None

    def execute(self, statement: str, parameters: Iterable[Any] = (), /) -> sqlite3.Cursor:
        cursor = self.cursor(factory=_InterleavingCursor)
        return cursor.execute(statement, parameters)


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
    database = _create_state(codex_home)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO threads VALUES (?, ?)", (THREAD_ID, str(rollout_path)))
    return database


def _create_state(codex_home: Path) -> Path:
    database = codex_home / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
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


def test_prepare_resume_rejects_multiple_exact_rollouts(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    rollout = _write_rollout(codex_home)
    duplicate_dir = codex_home / "sessions" / "2026" / "07" / "31"
    duplicate_dir.mkdir(parents=True)
    (duplicate_dir / rollout.name).write_bytes(rollout.read_bytes())

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="multiple"):
        codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID)


def test_prepare_resume_rejects_broken_state_database_link(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(codex_home)
    (codex_home / "state_5.sqlite").symlink_to(tmp_path / "missing.sqlite")

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="broken symlink"):
        codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID)


def test_prepare_resume_rejects_state_database_directory(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(codex_home)
    (codex_home / "state_5.sqlite").mkdir()

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="not a file"):
        codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID)


def test_prepare_resume_wraps_incompatible_state_schema(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(codex_home)
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="inspect Codex state"):
        codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID)


@pytest.mark.parametrize("store_current_path", [False, True])
def test_prepare_resume_accepts_state_without_stale_path(
    tmp_path: Path, *, store_current_path: bool
) -> None:
    codex_home = tmp_path / ".codex"
    rollout = _write_rollout(codex_home)
    database = _create_state(codex_home)
    if store_current_path:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO threads VALUES (?, ?)", (THREAD_ID, str(rollout.resolve()))
            )

    assert codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID) is True


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


def test_prepare_resume_concurrent_path_survives_cas_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / ".codex"
    _write_rollout(codex_home)
    stale_rollout = Path("/missing/old-rollout.jsonl")
    concurrent_rollout = Path("/concurrent/new-rollout.jsonl")
    database = _write_state(codex_home, stale_rollout)
    real_connect = sqlite3.connect

    def racing_connect(path: Path) -> sqlite3.Connection:
        connection = real_connect(path, factory=_InterleavingConnection)

        def replace_stale_path() -> None:
            with real_connect(database) as concurrent:
                concurrent.execute(
                    "UPDATE threads SET rollout_path = ? WHERE id = ?",
                    (str(concurrent_rollout), THREAD_ID),
                )

        connection.interleave = replace_stale_path
        return connection

    monkeypatch.setattr(
        codex_rollouts.sqlite3,
        "connect",
        racing_connect,
    )

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="state changed"):
        codex_rollouts.prepare_rollout_resume(codex_home, THREAD_ID)

    with real_connect(database) as verifier:
        assert verifier.execute(
            "SELECT rollout_path FROM threads WHERE id = ?", (THREAD_ID,)
        ).fetchone() == (str(concurrent_rollout),)
