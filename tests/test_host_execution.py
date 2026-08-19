"""Tests for direct host-execution session discovery."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from beartype import beartype
from conftest import make_settings

from pynchy.host.orchestrator import host_execution
from pynchy.host.orchestrator.host_execution import (
    HostExecutionCwd,
    HostRuntimeOperations,
    codex_thread_exists_in_host_runtime,
)


@dataclass(frozen=True)
class _ResolvedHostWorkspace:
    execution_mode: str
    cwd: str
    repo: list[str]


def _runtime_operations(
    settings,
    *,
    build_agent_environment=lambda **_kwargs: {},
    host_learning_vault=lambda _folder: None,
) -> HostRuntimeOperations:
    return HostRuntimeOperations(
        build_agent_environment=build_agent_environment,
        prepare_mcp=AsyncMock(),
        sessions_root=settings.data_dir / "sessions",
        project_root=settings.project_root,
        gateway_port=settings.gateway.port,
        prepare_host_codex_home=lambda folder, _plugins: (
            settings.data_dir / "sessions" / folder / ".codex"
        ),
        host_learning_vault=host_learning_vault,
        resolve_routed_host_cwd=lambda _folder, cwd, _repo_accesses, *, recovered: HostExecutionCwd(
            cwd
        ),
    )


def test_host_agent_turn_request_is_runtime_decoratable() -> None:
    """The public host-turn request boundary remains instrumented by Beartype."""
    assert callable(beartype(host_execution.HostAgentTurnRequest))


def test_routed_host_execution_resolves_selected_repository_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_cwd = tmp_path / "parent" / "tools"
    child_cwd = tmp_path / "worktrees" / "routed" / "tools"
    profile_repo = "owner/profile-repository"
    scheduled_override = "owner/scheduled-override"
    resolved = _ResolvedHostWorkspace("host", str(source_cwd), [profile_repo])
    resolver = MagicMock(
        return_value=HostExecutionCwd(child_cwd, ("child notice",), scheduled_override)
    )
    operations = _runtime_operations(make_settings())
    operations.resolve_routed_host_cwd = resolver
    monkeypatch.setattr(
        host_execution.workspace_config,
        "load_resolved_config",
        lambda _folder: resolved,
    )

    result = host_execution.host_execution_cwd(
        "host__thread_conversation-conv_routed",
        operations,
        repo_accesses=[scheduled_override],
        recovered=True,
    )

    assert result == HostExecutionCwd(child_cwd, ("child notice",), scheduled_override)
    resolver.assert_called_once_with(
        "host__thread_conversation-conv_routed",
        source_cwd,
        [scheduled_override],
        recovered=True,
    )


def test_routed_execution_without_policy_refuses_container_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = _runtime_operations(make_settings())
    monkeypatch.setattr(
        host_execution.workspace_config,
        "load_resolved_config",
        lambda _folder: None,
    )

    with pytest.raises(
        host_execution.HostExecutionCwdError,
        match="refusing container fallback",
    ):
        host_execution.host_execution_cwd(
            "host__thread_conversation-conv_missing-policy",
            operations,
            repo_accesses=[],
            recovered=True,
        )


@pytest.mark.parametrize(
    ("folder", "repo_accesses"),
    [
        ("host", ["owner/repo"]),
        ("host__thread_discord-review", ["owner/repo"]),
        ("host__thread_conversation-conv_no_repo", []),
    ],
)
def test_non_routed_or_no_repo_host_execution_keeps_configured_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    folder: str,
    repo_accesses: list[str],
) -> None:
    source_cwd = tmp_path / "parent"
    resolved = _ResolvedHostWorkspace("host", str(source_cwd), [])
    resolver = MagicMock(side_effect=AssertionError("must not resolve a repository worktree"))
    operations = _runtime_operations(make_settings())
    operations.resolve_routed_host_cwd = resolver
    monkeypatch.setattr(
        host_execution.workspace_config,
        "load_resolved_config",
        lambda _folder: resolved,
    )

    result = host_execution.host_execution_cwd(
        folder,
        operations,
        repo_accesses=repo_accesses,
        recovered=False,
    )

    assert result == HostExecutionCwd(source_cwd)
    resolver.assert_not_called()


def _write_rollout(
    codex_home: Path,
    thread_id: str,
    *,
    header_id: str | None = None,
    body: str = "",
) -> Path:
    rollout_dir = codex_home / "sessions" / "2026" / "07" / "14"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    header = {"type": "session_meta", "payload": {"id": header_id or thread_id}}
    rollout = rollout_dir / f"rollout-2026-07-14T07-27-55-{thread_id}.jsonl"
    rollout.write_text(json.dumps(header) + "\n" + body)
    return rollout


def _write_codex_state(codex_home: Path, rows: list[tuple[str, str]]) -> Path:
    database = codex_home / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
        connection.executemany("INSERT INTO threads VALUES (?, ?)", rows)
    return database


def test_host_codex_thread_exists_when_rollout_is_absent_from_stale_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "session_index.jsonl").write_text('{"id":"some-other-thread"}\n')
    _write_rollout(tmp_path, thread_id)

    assert codex_thread_exists_in_host_runtime(f"codex:gpt-5.5:{thread_id}")


def test_host_codex_thread_relocates_only_exact_stale_state_row(
    tmp_path: Path,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    other_thread = "019f6106-fd23-7292-bac5-7dbb7da29003"
    rollout = _write_rollout(tmp_path, thread_id)
    database = _write_codex_state(
        tmp_path,
        [
            (thread_id, f"/Users/old/.codex/sessions/{rollout.name}"),
            (other_thread, "/Users/old/.codex/sessions/other.jsonl"),
        ],
    )

    assert codex_thread_exists_in_host_runtime(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=tmp_path,
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT id, rollout_path FROM threads ORDER BY id").fetchall()
    assert rows == [
        (thread_id, str(rollout.resolve())),
        (other_thread, "/Users/old/.codex/sessions/other.jsonl"),
    ]


def test_host_codex_thread_is_missing_without_rollout_even_when_index_claims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "session_index.jsonl").write_text(f'{{"id":"{thread_id}"}}\n')

    assert not codex_thread_exists_in_host_runtime(f"codex:gpt-5.5:{thread_id}")


@pytest.mark.parametrize("content", ["", "{truncated"])
def test_host_codex_thread_is_missing_when_rollout_header_is_not_durable(
    tmp_path: Path,
    content: str,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    rollout_dir = tmp_path / "sessions" / "2026" / "07" / "14"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / f"rollout-2026-07-14T07-27-55-{thread_id}.jsonl").write_text(content)

    assert not codex_thread_exists_in_host_runtime(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=tmp_path,
    )


def test_host_codex_thread_is_missing_when_rollout_header_is_not_utf8(
    tmp_path: Path,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    rollout_dir = tmp_path / "sessions" / "2026" / "07" / "14"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / f"rollout-2026-07-14T07-27-55-{thread_id}.jsonl").write_bytes(b"\xff")

    assert not codex_thread_exists_in_host_runtime(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=tmp_path,
    )


def test_host_codex_thread_inspection_error_is_not_treated_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sessions").mkdir()

    def fail_inspection(_path: Path, _pattern: str):
        raise OSError("storage unavailable")

    monkeypatch.setattr(Path, "rglob", fail_inspection)

    with pytest.raises(host_execution.CodexRolloutInspectionError):
        codex_thread_exists_in_host_runtime(
            "codex:gpt-5.5:019f6106-fd23-7292-bac5-7dbb7da29002",
            codex_home=tmp_path,
        )


def test_admin_host_execution_uses_canonical_learning_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def build_agent_environment(**_kwargs: object) -> dict[str, str]:
        return {"OPENAI_BASE_URL": "http://gateway:4000"}

    settings = make_settings(data_dir=tmp_path / "data")
    env = host_execution.host_agent_env_vars(
        is_admin=True,
        group_folder="pynchy-dev",
        operations=_runtime_operations(
            settings,
            build_agent_environment=build_agent_environment,
            host_learning_vault=lambda _folder: tmp_path,
        ),
        automation_memory_dir=tmp_path / "automation-memory/job-security",
    )

    assert env["OPENAI_BASE_URL"] == "http://localhost:4000"
    assert env["PYNCHY_GROUP_FOLDER"] == "pynchy-dev"
    assert env["PYNCHY_IS_ADMIN"] == "1"
    assert env["OBSIDIAN_VAULT_PATH"] == str(tmp_path)
    assert env["PYNCHY_AUTOMATION_MEMORY_DIR"] == str(tmp_path / "automation-memory/job-security")
    assert env["PYNCHY_IPC_DIR"] == str(tmp_path / "data" / "ipc" / "pynchy-dev")
    assert env["PYNCHY_SKILLS_ROOT"] == str(
        settings.project_root / "data" / "personalization" / "skills"
    )
    assert env["GIT_CEILING_DIRECTORIES"] == env["PYNCHY_SKILLS_ROOT"]
    assert "PYNCHY_PROFILE_SKILLS_ROOT" not in env


def test_admin_host_execution_skips_missing_learning_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    env = host_execution.host_agent_env_vars(
        is_admin=True,
        group_folder="pynchy-dev",
        operations=_runtime_operations(settings),
    )

    assert "OBSIDIAN_VAULT_PATH" not in env


def test_non_admin_host_execution_propagates_hook_workspace_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    env = host_execution.host_agent_env_vars(
        is_admin=False,
        group_folder="review",
        operations=_runtime_operations(settings),
    )

    assert env["PYNCHY_GROUP_FOLDER"] == "review"
    assert env["PYNCHY_IS_ADMIN"] == "0"
    assert env["PYNCHY_IPC_DIR"] == str(tmp_path / "data" / "ipc" / "review")
