"""Behavior tests for less common public ``pynchy`` CLI paths."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import aiohttp
import pytest
from conftest import make_settings

from pynchy import __main__ as cli


@dataclass
class _Runtime:
    cli: str = "container-runtime"

    def __post_init__(self) -> None:
        self.ensure_running = Mock()


@dataclass(frozen=True)
class _RunResult:
    returncode: int


def test_default_cli_validates_personalization_before_running_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    mapping = {"validated": True}
    app_run = Mock()

    async def run_app() -> None:
        await asyncio.sleep(0)
        app_run()

    class _App:
        run = staticmethod(run_app)

    monkeypatch.setattr(sys, "argv", ["pynchy"])
    with (
        patch("dotenv.load_dotenv") as load_dotenv,
        patch(
            "pynchy.config.api.validate_personalization_tree", return_value=mapping
        ) as validate_tree,
        patch(
            "pynchy.config.api.validate_settings_mapping", return_value=settings
        ) as validate_settings,
        patch("pynchy.config.api.validate_litellm_model_names") as validate_models,
        patch("pynchy.logger.configure_error_log") as configure_log,
        patch("pynchy.host.orchestrator.app.PynchyApp", return_value=_App()),
    ):
        cli.main()

    load_dotenv.assert_called_once_with()
    validate_tree.assert_called_once_with(Path.cwd(), Path("data/personalization"))
    validate_settings.assert_called_once_with(mapping)
    validate_models.assert_called_once_with(
        Path("data/personalization") / "litellm.yaml",
        settings.configured_agent_models(),
    )
    configure_log.assert_called_once_with(Path("logs/pynchy.error.log"))
    app_run.assert_called_once_with()


def test_default_cli_reports_personalization_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["pynchy"])
    monkeypatch.setattr(
        "pynchy.config.api.validate_personalization_tree",
        Mock(side_effect=ValueError("bad mapping")),
    )

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 2
    assert capsys.readouterr().err == "Personalization validation failed: bad mapping\n"


@pytest.mark.parametrize(
    ("cleanup_results", "build_code", "expected_code"),
    [((False,), 0, 1), ((True, True), 7, 7), ((True, False), 0, 1)],
)
def test_build_cli_handles_cleanup_failures_and_build_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    cleanup_results: tuple[bool, ...],
    build_code: int,
    expected_code: int,
) -> None:
    container_dir = tmp_path / "src" / "pynchy" / "agent"
    container_dir.mkdir(parents=True)
    (container_dir / "Dockerfile").write_text("FROM scratch\n")
    runtime = _Runtime()
    settings = make_settings(project_root=tmp_path)
    monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["pynchy", "build"])

    with (
        patch("pynchy.plugins.runtimes.detection.configure_runtime_override"),
        patch("pynchy.plugins.runtimes.detection.get_runtime", return_value=runtime),
        patch(
            "pynchy.plugins.runtimes.cleanup.cleanup_runtime_build_state",
            side_effect=cleanup_results,
        ),
        patch("pynchy.plugins.runtimes.cleanup.cleanup_runtime_builder") as cleanup_builder,
        patch("pynchy.__main__.subprocess.run", return_value=_RunResult(build_code)),
        pytest.raises(SystemExit) as exited,
    ):
        cli.main()

    assert exited.value.code == expected_code
    if build_code:
        cleanup_builder.assert_called_once_with(runtime)
    else:
        cleanup_builder.assert_not_called()
    if cleanup_results == (False,):
        assert "Could not clean stale container build state" in capsys.readouterr().err
    elif cleanup_results == (True, False):
        assert "Could not clean container build state after the build" in capsys.readouterr().err


def test_validate_personalization_cli_prints_the_validated_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    settings = make_settings()
    mapping = {"validated": True}
    path = tmp_path / "personalization"
    monkeypatch.setattr(sys, "argv", ["pynchy", "validate-personalization", str(path)])
    monkeypatch.setattr("pynchy.config.api.validate_personalization_tree", lambda *_: mapping)
    monkeypatch.setattr("pynchy.config.api.validate_settings_mapping", lambda _: settings)
    monkeypatch.setattr("pynchy.config.api.validate_litellm_model_names", lambda *_: None)

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 0
    assert capsys.readouterr().out == (
        f"Personalization valid: {path.resolve()} (0 automation(s), 0 workspace(s))\n"
    )


def test_prune_cli_reports_when_nothing_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(
        "pynchy.config.api.get_settings", lambda: make_settings(project_root=tmp_path)
    )
    monkeypatch.setattr(sys, "argv", ["pynchy", "prune-migration-backups", str(backups)])

    cli.main()

    assert capsys.readouterr().out == (
        f"Migration backups: {backups}\n"
        "Keeping 0 backup(s).\n"
        "No older backup directories to remove.\n"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"workspaces": {}},
        {"workspaces": [1]},
        {"workspaces": [{"capabilities": {}}]},
        {"workspaces": [{"capabilities": [1]}]},
    ],
)
def test_doctor_rejects_malformed_workspace_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    payload: object,
) -> None:
    monkeypatch.setattr("pynchy.__main__._fetch_control_payload", Mock(return_value=payload))
    monkeypatch.setattr(sys, "argv", ["pynchy", "doctor"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    assert capsys.readouterr().err.startswith("Capability doctor failed:")


def test_doctor_reports_an_empty_capability_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "pynchy.__main__._fetch_control_payload", Mock(return_value={"workspaces": []})
    )
    monkeypatch.setattr(sys, "argv", ["pynchy", "doctor"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 0
    assert capsys.readouterr().out == "No configured workspace capability snapshots.\n"


def test_fetch_control_payload_uses_bearer_auth_for_tcp_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret-token\n")
    Path(token_file).chmod(stat.S_IRUSR | stat.S_IWUSR)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    response.read.return_value = json.dumps({"ok": True}).encode()
    opened = Mock(return_value=response)
    monkeypatch.setattr(cli.urllib.request, "urlopen", opened)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pynchy",
            "--host",
            "https://example.test/",
            "--token-file",
            str(token_file),
            "doctor",
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 0
    request = opened.call_args.args[0]
    assert isinstance(request, cli.urllib.request.Request)
    assert request.full_url == "https://example.test/capabilities"
    assert request.get_header("Authorization") == "Bearer secret-token"


def test_fetch_control_payload_reports_when_target_selection_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("pynchy.__main__._control_client_target", lambda *_: (None, None))
    monkeypatch.setattr(sys, "argv", ["pynchy", "status"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    assert (
        capsys.readouterr().err
        == "Status failed: No TCP host or Unix socket selected for the control-plane client\n"
    )


def test_doctor_can_read_from_an_explicit_unix_socket(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "pynchy.sock"
    socket_path.touch()

    class _Response:
        async def __aenter__(self) -> _Response:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> object:
            return {"workspaces": []}

    class _Session:
        def __init__(self, *, connector: object, headers: dict[str, str] | None) -> None:
            assert connector == "connector"
            assert headers is None
            self.response = _Response()
            self.response = _Response()

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def request(self, *_args: object, **_kwargs: object) -> _Response:
            return self.response

    monkeypatch.setattr(aiohttp, "UnixConnector", lambda *, path: "connector")
    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(sys, "argv", ["pynchy", "--socket", str(socket_path), "doctor"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 0
    assert capsys.readouterr().out == "No configured workspace capability snapshots.\n"


def test_control_command_reports_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "pynchy.__main__._fetch_control_payload", Mock(side_effect=OSError("offline"))
    )
    monkeypatch.setattr(sys, "argv", ["pynchy", "deploy"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    assert capsys.readouterr().err == "Deploy failed: offline\n"


def test_doctor_reports_unix_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "pynchy.sock"
    socket_path.touch()

    class _Response:
        async def __aenter__(self) -> _Response:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            raise aiohttp.ClientError("offline")

        async def json(self) -> object:
            return {"ok": True}

    class _Session:
        def __init__(self, *, connector: object, headers: dict[str, str] | None) -> None:
            assert connector == "connector"
            assert headers is None
            self.response = _Response()

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def request(self, *_args: object, **_kwargs: object) -> _Response:
            return self.response

    monkeypatch.setattr(aiohttp, "UnixConnector", lambda *, path: "connector")
    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(sys, "argv", ["pynchy", "--socket", str(socket_path), "doctor"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    assert capsys.readouterr().err == "Capability doctor failed: offline\n"
