"""Tests for direct host-execution session discovery."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from beartype import beartype
from conftest import make_settings

from pynchy.host.learning.paths import LearningPaths
from pynchy.host.orchestrator import codex_rollouts, host_execution
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


def test_admin_host_execution_uses_full_learning_vault_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def build_agent_environment(**_kwargs: object) -> dict[str, str]:
        return {"OPENAI_BASE_URL": "http://gateway:4000"}

    learning_paths = LearningPaths(
        profile="default",
        profile_slug="default",
        vault_root=tmp_path,
        vault_mount_path="/workspace/vault",
        profile_root=tmp_path / "profiles" / "default",
        memory_root=tmp_path / "profiles" / "default" / "memory",
        vault_mirror_root=tmp_path / "data" / "learning" / "vault-mirrors" / "default",
        host_vault_mirror_root=tmp_path / "data" / "learning" / "host-vault-mirrors" / "default",
        mounted_profile_root="/workspace/vault/profiles/default",
        mounted_memory_root="/workspace/vault/profiles/default/memory",
    )
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


def test_admin_host_execution_skips_missing_full_learning_vault_mirror(
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


def test_host_codex_thread_migrates_from_legacy_global_home(tmp_path: Path) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    legacy_home = tmp_path / "legacy-codex"
    scoped_home = tmp_path / "scoped-codex"
    _write_rollout(legacy_home, thread_id)

    migrated = host_execution.migrate_host_codex_thread(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=scoped_home,
        legacy_codex_home=legacy_home,
    )

    assert migrated is True
    assert host_execution.codex_thread_exists_in_host_runtime(
        f"codex:gpt-5.5:{thread_id}", codex_home=scoped_home
    )


def test_host_codex_thread_migrates_from_scoped_sibling_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    settings = make_settings(data_dir=tmp_path / "data")
    source_home = settings.data_dir / "sessions" / "old-workspace" / ".codex"
    target_home = settings.data_dir / "sessions" / "new-workspace" / ".codex"
    source = _write_rollout(source_home, thread_id, body='{"type":"response"}\n')

    migrated = host_execution.migrate_host_codex_thread(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=target_home,
        sessions_root=settings.data_dir / "sessions",
        legacy_codex_home=tmp_path / "empty-legacy-home",
    )

    destination = target_home / "sessions" / source.relative_to(source_home / "sessions")
    assert migrated is True
    assert destination.read_bytes() == source.read_bytes()
    assert source.exists()
    assert source != destination


def test_host_codex_thread_rejects_divergent_global_and_sibling_rollouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    settings = make_settings(data_dir=tmp_path / "data")
    legacy_home = tmp_path / "legacy-codex"
    sibling_home = settings.data_dir / "sessions" / "old-workspace" / ".codex"
    target_home = settings.data_dir / "sessions" / "new-workspace" / ".codex"
    global_source = _write_rollout(legacy_home, thread_id, body="global history\n")
    sibling_source = _write_rollout(sibling_home, thread_id, body="sibling history\n")

    with pytest.raises(
        host_execution.CodexRolloutInspectionError,
        match="Divergent Codex rollout sources",
    ):
        host_execution.migrate_host_codex_thread(
            f"codex:gpt-5.5:{thread_id}",
            codex_home=target_home,
            sessions_root=settings.data_dir / "sessions",
            legacy_codex_home=legacy_home,
        )

    assert global_source.exists()
    assert sibling_source.exists()
    assert not (target_home / "sessions").exists()


def test_host_codex_thread_rejects_divergent_sibling_rollouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    settings = make_settings(data_dir=tmp_path / "data")
    first_home = settings.data_dir / "sessions" / "first-workspace" / ".codex"
    second_home = settings.data_dir / "sessions" / "second-workspace" / ".codex"
    target_home = settings.data_dir / "sessions" / "new-workspace" / ".codex"
    first_source = _write_rollout(first_home, thread_id, body="first history\n")
    second_source = _write_rollout(second_home, thread_id, body="second history\n")

    with pytest.raises(
        host_execution.CodexRolloutInspectionError,
        match="Divergent Codex rollout sources",
    ):
        host_execution.migrate_host_codex_thread(
            f"codex:gpt-5.5:{thread_id}",
            codex_home=target_home,
            sessions_root=settings.data_dir / "sessions",
            legacy_codex_home=tmp_path / "empty-legacy-home",
        )

    assert first_source.exists()
    assert second_source.exists()
    assert not (target_home / "sessions").exists()


def test_host_codex_thread_accepts_identical_global_and_sibling_rollouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    settings = make_settings(data_dir=tmp_path / "data")
    legacy_home = tmp_path / "legacy-codex"
    first_home = settings.data_dir / "sessions" / "first-workspace" / ".codex"
    second_home = settings.data_dir / "sessions" / "second-workspace" / ".codex"
    target_home = settings.data_dir / "sessions" / "new-workspace" / ".codex"
    sources = (
        _write_rollout(legacy_home, thread_id, body="identical history\n"),
        _write_rollout(first_home, thread_id, body="identical history\n"),
        _write_rollout(second_home, thread_id, body="identical history\n"),
    )

    assert host_execution.migrate_host_codex_thread(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=target_home,
        sessions_root=settings.data_dir / "sessions",
        legacy_codex_home=legacy_home,
    )

    destination = target_home / "sessions" / sources[0].relative_to(legacy_home / "sessions")
    assert destination.read_bytes() == sources[0].read_bytes()
    assert all(source.exists() for source in sources)


def test_host_codex_thread_ignores_sibling_rollout_with_mismatched_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    settings = make_settings(data_dir=tmp_path / "data")
    source_home = settings.data_dir / "sessions" / "old-workspace" / ".codex"
    target_home = settings.data_dir / "sessions" / "new-workspace" / ".codex"
    source = _write_rollout(source_home, thread_id, header_id="different-thread")

    migrated = host_execution.migrate_host_codex_thread(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=target_home,
        sessions_root=settings.data_dir / "sessions",
        legacy_codex_home=tmp_path / "empty-legacy-home",
    )

    assert migrated is False
    assert source.exists()
    assert not (target_home / "sessions").exists()


def test_host_codex_thread_rejects_sibling_sessions_symlink_outside_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    settings = make_settings(data_dir=tmp_path / "data")
    workspace = settings.data_dir / "sessions" / "old-workspace"
    outside_home = tmp_path / "outside" / ".codex"
    _write_rollout(outside_home, thread_id)
    (workspace / ".codex").mkdir(parents=True)
    (workspace / ".codex" / "sessions").symlink_to(
        outside_home / "sessions",
        target_is_directory=True,
    )
    target_home = settings.data_dir / "sessions" / "new-workspace" / ".codex"

    with pytest.raises(host_execution.CodexRolloutInspectionError):
        host_execution.migrate_host_codex_thread(
            f"codex:gpt-5.5:{thread_id}",
            codex_home=target_home,
            sessions_root=settings.data_dir / "sessions",
            legacy_codex_home=tmp_path / "empty-legacy-home",
        )

    assert not (target_home / "sessions").exists()


def test_host_codex_thread_sibling_path_error_is_not_treated_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    settings = make_settings(data_dir=tmp_path / "data")
    sessions_root = settings.data_dir / "sessions"
    sessions_root.mkdir(parents=True)
    target_home = sessions_root / "new-workspace" / ".codex"
    original_iterdir = Path.iterdir

    def fail_sibling_scan(path: Path):
        if path == sessions_root:
            raise OSError("storage unavailable")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_sibling_scan)

    with pytest.raises(host_execution.CodexRolloutInspectionError):
        host_execution.migrate_host_codex_thread(
            f"codex:gpt-5.5:{thread_id}",
            codex_home=target_home,
            sessions_root=settings.data_dir / "sessions",
            legacy_codex_home=tmp_path / "empty-legacy-home",
        )

    assert not (target_home / "sessions").exists()


def test_host_codex_thread_collision_preserves_both_raw_rollouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    settings = make_settings(data_dir=tmp_path / "data")
    source_home = settings.data_dir / "sessions" / "old-workspace" / ".codex"
    target_home = settings.data_dir / "sessions" / "new-workspace" / ".codex"
    source = _write_rollout(source_home, thread_id, body="source raw\n")
    colliding = _write_rollout(
        target_home,
        thread_id,
        header_id="different-thread",
        body="target raw\n",
    )
    source_raw = source.read_bytes()
    target_raw = colliding.read_bytes()

    assert host_execution.migrate_host_codex_thread(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=target_home,
        sessions_root=settings.data_dir / "sessions",
        legacy_codex_home=tmp_path / "empty-legacy-home",
    )

    digest = hashlib.sha256(source_raw).hexdigest()
    recovered = target_home / "sessions" / "recovered" / thread_id / digest / source.name
    assert source.read_bytes() == source_raw
    assert colliding.read_bytes() == target_raw
    assert recovered.read_bytes() == source_raw


def test_host_codex_thread_fsyncs_copy_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    legacy_home = tmp_path / "legacy-codex"
    scoped_home = tmp_path / "scoped-codex"
    _write_rollout(legacy_home, thread_id)
    fsynced: list[int] = []
    monkeypatch.setattr(codex_rollouts.os, "fsync", fsynced.append)

    assert host_execution.migrate_host_codex_thread(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=scoped_home,
        legacy_codex_home=legacy_home,
    )

    assert len(fsynced) == 1


def test_host_codex_thread_copy_error_is_not_treated_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "019f6106-fd23-7292-bac5-7dbb7da29002"
    legacy_home = tmp_path / "legacy-codex"
    scoped_home = tmp_path / "scoped-codex"
    source = _write_rollout(legacy_home, thread_id)

    def fail_copy(*_args) -> None:
        Path(_args[1]).write_text('{"type":"session_meta"', encoding="utf-8")
        raise OSError("disk unavailable")

    monkeypatch.setattr(codex_rollouts.shutil, "copy2", fail_copy)

    with pytest.raises(host_execution.CodexRolloutInspectionError):
        host_execution.migrate_host_codex_thread(
            f"codex:gpt-5.5:{thread_id}",
            codex_home=scoped_home,
            legacy_codex_home=legacy_home,
        )
    assert not host_execution.codex_thread_exists_in_host_runtime(
        f"codex:gpt-5.5:{thread_id}",
        codex_home=scoped_home,
    )
    assert source.exists()
