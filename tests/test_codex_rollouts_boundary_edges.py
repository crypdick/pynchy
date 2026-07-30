"""Boundary behavior for confined Codex rollout discovery and migration."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pynchy.host.orchestrator import codex_rollouts

THREAD_ID = "019f6106-fd23-7292-bac5-7dbb7da29002"


def _write_rollout(home: Path, *, thread_id: str = THREAD_ID, body: str = "") -> Path:
    directory = home / "sessions" / "2026" / "07" / "30"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-07-30T07-27-55-{thread_id}.jsonl"
    path.write_text(
        '{"type":"session_meta","payload":{"id":"' + thread_id + '"}}\n' + body,
        encoding="utf-8",
    )
    return path


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


def test_migrate_rollout_rejects_unreadable_source_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_rollout(tmp_path / "legacy")
    monkeypatch.setattr(
        codex_rollouts,
        "_rollout_digest",
        lambda _path: (_ for _ in ()).throw(OSError("digest unavailable")),
    )

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="compare"):
        codex_rollouts.migrate_rollout(
            THREAD_ID,
            codex_home=tmp_path / "target",
            legacy_codex_home=tmp_path / "legacy",
            scoped_sessions_root=tmp_path / "scoped-sessions",
        )
    assert source.exists()


def test_migrate_rollout_rejects_unreadable_scoped_sessions_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoped = tmp_path / "scoped-sessions"
    scoped.mkdir()
    original_resolve = Path.resolve

    def fail_scoped_root(path: Path, *args, **kwargs):
        if path == scoped:
            raise OSError("scoped root unavailable")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_scoped_root)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="scoped Codex homes"):
        codex_rollouts.migrate_rollout(
            THREAD_ID,
            codex_home=scoped / "new-workspace" / ".codex",
            legacy_codex_home=tmp_path / "empty-legacy",
            scoped_sessions_root=scoped,
        )


def test_migrate_rollout_rejects_broken_sibling_sessions_link(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped-sessions"
    scoped.mkdir()
    workspace = scoped / "old-workspace"
    workspace.mkdir()
    (workspace / ".codex" / "sessions").parent.mkdir(parents=True)
    (workspace / ".codex" / "sessions").symlink_to(tmp_path / "missing", target_is_directory=True)
    target = scoped / "new-workspace" / ".codex"

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="Broken sibling"):
        codex_rollouts.migrate_rollout(
            THREAD_ID,
            codex_home=target,
            legacy_codex_home=tmp_path / "empty-legacy",
            scoped_sessions_root=scoped,
        )


def test_migrate_rollout_ignores_missing_sibling_sessions(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped-sessions"
    (scoped / "old-workspace").mkdir(parents=True)

    assert (
        codex_rollouts.migrate_rollout(
            THREAD_ID,
            codex_home=scoped / "new-workspace" / ".codex",
            legacy_codex_home=tmp_path / "empty-legacy",
            scoped_sessions_root=scoped,
        )
        is None
    )


def test_migrate_rollout_rejects_unreadable_sibling_sessions_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoped = tmp_path / "scoped-sessions"
    candidate = scoped / "old-workspace" / ".codex" / "sessions"
    candidate.mkdir(parents=True)
    target = scoped / "new-workspace" / ".codex"
    original_resolve = Path.resolve

    def fail_candidate(path: Path, *args, **kwargs):
        if path == candidate:
            raise OSError("sibling path unavailable")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_candidate)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="sibling Codex"):
        codex_rollouts.migrate_rollout(
            THREAD_ID,
            codex_home=target,
            legacy_codex_home=tmp_path / "empty-legacy",
            scoped_sessions_root=scoped,
        )


def test_migrate_rollout_uses_primary_destination_when_available(tmp_path: Path) -> None:
    source = _write_rollout(tmp_path / "legacy")
    target = tmp_path / "target" / ".codex"

    destination = codex_rollouts.migrate_rollout(
        THREAD_ID,
        codex_home=target,
        legacy_codex_home=tmp_path / "legacy",
        scoped_sessions_root=tmp_path / "scoped-sessions",
    )

    assert destination == target / "sessions" / source.relative_to(tmp_path / "legacy" / "sessions")


def test_migrate_rollout_rejects_source_and_destination_alias(tmp_path: Path) -> None:
    home = tmp_path / "same-home"
    _write_rollout(home)

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="outside"):
        codex_rollouts.migrate_rollout(
            THREAD_ID,
            codex_home=home,
            legacy_codex_home=home,
            scoped_sessions_root=tmp_path / "scoped-sessions",
        )


def test_migrate_rollout_rejects_recovered_destination_that_resolves_to_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy"
    target = tmp_path / "target"
    source = _write_rollout(legacy)
    primary = target / "sessions" / "2026" / "07" / "30" / source.name
    primary.parent.mkdir(parents=True)
    primary.write_text("existing primary", encoding="utf-8")
    recovered = target / "sessions" / "recovered" / THREAD_ID / "digest" / source.name
    source_resolved = source.resolve()
    original_resolve = Path.resolve
    destination_resolves = 0

    def resolve_as_source(path: Path, *args, **kwargs):
        nonlocal destination_resolves
        if path == recovered:
            destination_resolves += 1
            if destination_resolves == 2:
                return source_resolved
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_as_source)
    monkeypatch.setattr(codex_rollouts, "_rollout_digest", lambda _path: "digest")

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="outside"):
        codex_rollouts.migrate_rollout(
            THREAD_ID,
            codex_home=target,
            legacy_codex_home=legacy,
            scoped_sessions_root=tmp_path / "scoped-sessions",
        )
    assert destination_resolves == 2


def test_migrate_rollout_rejects_existing_recovered_destination(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    target = tmp_path / "target"
    source = _write_rollout(legacy)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    primary = target / "sessions" / "2026" / "07" / "30" / source.name
    primary.parent.mkdir(parents=True)
    primary.write_text("existing primary", encoding="utf-8")
    recovered = target / "sessions" / "recovered" / THREAD_ID / digest
    recovered.mkdir(parents=True)
    (recovered / source.name).write_text("already recovered", encoding="utf-8")

    with pytest.raises(codex_rollouts.CodexRolloutInspectionError, match="scope"):
        codex_rollouts.migrate_rollout(
            THREAD_ID,
            codex_home=target,
            legacy_codex_home=legacy,
            scoped_sessions_root=tmp_path / "scoped-sessions",
        )
