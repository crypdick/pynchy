"""Tests for direct host-execution session discovery."""

from pathlib import Path

import pytest

from pynchy.host.orchestrator import host_execution
from pynchy.host.orchestrator.host_execution import codex_thread_exists_in_host_runtime


def _write_rollout(codex_home: Path, thread_id: str) -> None:
    rollout_dir = codex_home / "sessions" / "2026" / "07" / "14"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / f"rollout-2026-07-14T07-27-55-{thread_id}.jsonl").write_text("")


def test_host_codex_thread_exists_when_rollout_is_absent_from_stale_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "session_index.jsonl").write_text('{"id":"some-other-thread"}\n')
    _write_rollout(tmp_path, thread_id)

    assert codex_thread_exists_in_host_runtime(f"codex:gpt-5.5:{thread_id}")


def test_host_codex_thread_is_missing_without_rollout_even_when_index_claims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "session_index.jsonl").write_text(f'{{"id":"{thread_id}"}}\n')

    assert not codex_thread_exists_in_host_runtime(f"codex:gpt-5.5:{thread_id}")


def test_admin_host_execution_uses_full_learning_vault_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        host_execution,
        "build_agent_env_vars",
        lambda **_kwargs: {"OPENAI_BASE_URL": "http://gateway:4000"},
    )
    monkeypatch.setattr(host_execution, "resolve_learning_paths", lambda _folder: object())
    monkeypatch.setattr(host_execution, "prepare_full_vault_host_root", lambda _paths: tmp_path)

    env = host_execution.host_agent_env_vars(is_admin=True, group_folder="pynchy-dev")

    assert env["OPENAI_BASE_URL"] == "http://localhost:4000"
    assert env["OBSIDIAN_VAULT_PATH"] == str(tmp_path)


def test_admin_host_execution_skips_missing_full_learning_vault_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_execution, "build_agent_env_vars", lambda **_kwargs: {})
    monkeypatch.setattr(host_execution, "resolve_learning_paths", lambda _folder: object())
    monkeypatch.setattr(host_execution, "prepare_full_vault_host_root", lambda _paths: None)

    env = host_execution.host_agent_env_vars(is_admin=True, group_folder="pynchy-dev")

    assert "OBSIDIAN_VAULT_PATH" not in env
