from __future__ import annotations

import asyncio
import sqlite3
import stat

from scripts import new_feature_sandbox as sandbox


def test_prepare_creates_isolated_configuration_and_databases(tmp_path, monkeypatch) -> None:
    source_root = tmp_path / "control"
    source_root.mkdir()
    source_root.joinpath("litellm_config.yaml").write_text(
        "model_list:\n"
        "  - model_name: sandbox\n"
        "    litellm_params:\n"
        "      model: openai/gpt-5-mini\n"
        "      api_key: os.environ/OPENAI_API_KEY\n"
    )
    source_root.joinpath(".env").write_text(
        "OPENAI_API_KEY=provider-secret\nSLACK_BOT_TOKEN=channel-secret\n"  # pragma: allowlist secret  # noqa: E501
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("NEW_FEATURE_REPO_ROOT", str(source_root))
    monkeypatch.setenv("SERVER__PORT", "18484")
    monkeypatch.setenv("GATEWAY__PORT", "14010")
    monkeypatch.setenv("NEW_FEATURE_TEMPORAL_PORT", "17233")
    monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy_test_feature")

    state = sandbox._write_runtime_config(source_root)
    (worktree / "data").mkdir()
    asyncio.run(sandbox._initialize_databases())

    assert state == {
        "namespace": "pynchy_test_feature",
        "server_port": 18484,
        "gateway_port": 14010,
        "temporal_port": 17233,
    }
    assert 'temporal_address = "127.0.0.1:17233"' in worktree.joinpath("config.toml").read_text()
    dotenv = worktree.joinpath(".env").read_text()
    assert "OPENAI_API_KEY" in dotenv
    assert "SLACK_BOT_TOKEN" not in dotenv
    assert stat.S_IMODE(worktree.joinpath(".env").stat().st_mode) == 0o600

    with sqlite3.connect(worktree / "data/messages.db") as database:
        assert database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone() == (1,)
    with sqlite3.connect(worktree / "data/memories.db") as database:
        assert database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone() == (1,)


def test_remove_runtime_resources_removes_namespaced_volume(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return sandbox.subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(sandbox.subprocess, "run", run)

    sandbox._remove_runtime_resources("pynchy_feature_test")

    assert [
        "/usr/bin/docker",
        "volume",
        "rm",
        "pynchy_feature_test-litellm-db-data",
    ] in calls
