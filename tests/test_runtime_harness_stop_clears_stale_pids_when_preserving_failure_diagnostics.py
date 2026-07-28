"""Contract tests for the hermetic deterministic runtime harness."""

from __future__ import annotations

import contextlib
import json
import subprocess  # noqa: S404 - test helpers record mocked subprocess results only.
from typing import TYPE_CHECKING

import pytest
from scripts import runtime_harness as harness

from tests.runtime_harness_support import (
    _process_marker,
    _runtime_root,
    _spec,
    _wait_for_process_group_to_disappear,
    _write_runtime_state,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_stop_clears_stale_pids_when_preserving_failure_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    pynchy_marker = _process_marker("pynchy")
    temporal_marker = _process_marker("temporal")
    image = "pynchy-runtime-agent:pynchy-runtime-test-0123456789abcdef"
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": spec.namespace,
            "pynchy_pid": 1234,
            "temporal_pid": 5678,
            "pynchy_marker": pynchy_marker,
            "temporal_marker": temporal_marker,
            "agent_image": image,
        },
    )
    stopped_pids: list[tuple[object, object, bool]] = []
    removed_resources: list[object] = []
    removed_images: list[tuple[object, object]] = []
    monkeypatch.setattr(
        "scripts.runtime_harness._stop_pid",
        lambda pid, marker, *, after_term=None: stopped_pids.append(
            (pid, marker, after_term is not None)
        ),
    )
    monkeypatch.setattr(
        "scripts.runtime_harness._remove_runtime_resources", removed_resources.append
    )
    monkeypatch.setattr(
        "scripts.runtime_harness._remove_runtime_agent_image",
        lambda namespace, image: removed_images.append((namespace, image)),
    )

    harness.stop(spec.root, preserve_state=True)

    preserved_state = json.loads(spec.state_path.read_text())
    assert "pynchy_pid" not in preserved_state
    assert "temporal_pid" not in preserved_state
    assert "pynchy_marker" not in preserved_state
    assert "temporal_marker" not in preserved_state
    assert stopped_pids == [
        (1234, pynchy_marker, True),
        (5678, temporal_marker, False),
    ]
    assert removed_resources == [spec.namespace]
    assert removed_images == [(spec.namespace, image)]


def test_stop_reaps_a_terminated_marked_harness_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )
    process_group_signals: list[tuple[int, int]] = []

    monkeypatch.setattr("scripts.runtime_harness._process_has_marker", lambda *_: True)
    monkeypatch.setattr("scripts.runtime_harness._process_group_has_live_member", lambda _: False)
    monkeypatch.setattr(
        harness.os,
        "killpg",
        lambda pid, signal: process_group_signals.append((pid, signal)),
    )
    monkeypatch.setattr(harness.os, "waitpid", lambda pid, _flags: (pid, 0))

    harness.stop(spec.root)

    assert process_group_signals == [(1234, harness.signal.SIGTERM)]


def test_stop_never_signals_a_reused_or_unmarked_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )
    checks: list[tuple[int, str]] = []

    def process_has_marker(pid: int, value: str) -> bool:
        checks.append((pid, value))
        return False

    monkeypatch.setattr("scripts.runtime_harness._process_has_marker", process_has_marker)
    monkeypatch.setattr(harness.os, "killpg", lambda *_args: pytest.fail("must not signal"))

    harness.stop(spec.root)

    assert checks == [(1234, marker)]


def test_stop_removes_the_interactive_agent_before_waiting_for_its_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    events: list[str] = []
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": spec.namespace,
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )

    monkeypatch.setattr("scripts.runtime_harness._process_has_marker", lambda *_: True)
    monkeypatch.setattr("scripts.runtime_harness._process_group_has_live_member", lambda _: False)
    monkeypatch.setattr(
        harness.os,
        "killpg",
        lambda _pid, _signal: events.append("term"),
    )
    monkeypatch.setattr(
        "scripts.runtime_harness._remove_runtime_agent_container",
        lambda _: events.append("cleanup"),
    )
    monkeypatch.setattr("scripts.runtime_harness._remove_runtime_resources", lambda _: None)
    monkeypatch.setattr("scripts.runtime_harness._remove_runtime_agent_image", lambda *_: None)

    harness.stop(spec.root)

    assert events == ["term", "cleanup"]


def test_stop_treats_a_process_group_with_only_a_zombie_leader_as_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )
    calls: list[list[str]] = []
    signals: list[int] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["/usr/bin/ps", "-ww"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"/bin/sh -c ... {marker} ...")
        return subprocess.CompletedProcess(command, 0, stdout="1234 Zs\n")

    monkeypatch.setattr(harness.os, "killpg", lambda _pid, signal: signals.append(signal))
    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/ps")
    monkeypatch.setattr(harness.subprocess, "run", run)

    harness.stop(spec.root)

    assert calls == [
        ["/usr/bin/ps", "-ww", "-p", "1234", "-o", "command="],
        ["/usr/bin/ps", "-eo", "pgid=,stat="],
    ]
    assert signals == [harness.signal.SIGTERM, 0]


def test_stop_treats_a_permission_denied_zombie_group_probe_as_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Darwin can deny killpg(..., 0) while ps still proves only zombies remain."""
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )
    calls: list[list[str]] = []
    signals: list[int] = []
    waitpid_calls: list[tuple[int, int]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["/usr/bin/ps", "-ww"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"/bin/sh -c ... {marker} ...")
        return subprocess.CompletedProcess(command, 0, stdout="1234 Zs\n")

    def killpg(_pid: int, signal: int) -> None:
        signals.append(signal)
        if signal == 0:
            raise PermissionError

    def waitpid(pid: int, flags: int) -> tuple[int, int]:
        waitpid_calls.append((pid, flags))
        return (0, 0) if len(waitpid_calls) == 1 else (pid, 0)

    monkeypatch.setattr(harness.os, "killpg", killpg)
    monkeypatch.setattr(harness.os, "waitpid", waitpid)
    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/ps")
    monkeypatch.setattr(harness.subprocess, "run", run)

    harness.stop(spec.root)

    assert calls == [
        ["/usr/bin/ps", "-ww", "-p", "1234", "-o", "command="],
        ["/usr/bin/ps", "-eo", "pgid=,stat="],
    ]
    assert signals == [harness.signal.SIGTERM, 0]
    assert waitpid_calls == [(1234, harness.os.WNOHANG), (1234, harness.os.WNOHANG)]


def test_stop_escalates_for_a_term_ignoring_group_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead shell leader must not hide a still-running child from teardown."""
    marker = _process_marker("pynchy")
    root = _runtime_root(tmp_path)
    spec = _spec(root)
    process = subprocess.Popen(  # noqa: S603 - fixed local test supervisor command.
        [
            "/bin/sh",
            "-c",
            '"$@" &\nchild=$!\nwait "$child"\n',
            marker,
            "/bin/sh",
            "-c",
            "trap '' TERM; sleep 30 & wait",
        ],
        cwd=root,
        env={"PATH": "/usr/bin:/bin"},
        start_new_session=True,
    )
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": process.pid,
            "pynchy_marker": marker,
        },
    )
    monkeypatch.setattr("scripts.runtime_harness._STOP_TIMEOUT_SECONDS", 0.5)

    try:
        harness.stop(root)
        _wait_for_process_group_to_disappear(process.pid)
    finally:
        with contextlib.suppress(PermissionError, ProcessLookupError):
            harness.os.killpg(process.pid, harness.signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            harness.os.waitpid(process.pid, 0)


def test_setup_supervises_runtime_children_with_unique_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    captured: list[dict[str, object]] = []

    class FakeProcess:
        pid = 1234

        def poll(self) -> None:
            return None

    def popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured.append({"command": command, **kwargs})
        return FakeProcess()

    monkeypatch.setattr("scripts.runtime_harness._initialize_runtime_data", lambda _: None)
    monkeypatch.setattr("scripts.runtime_harness._start_fake_openai", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._ensure_runtime_agent_image", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._wait_for_port", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._wait_for_runtime", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(harness.subprocess, "Popen", popen)

    harness.setup(spec)

    assert len(captured) == 2
    for process in captured:
        command = process["command"]
        assert isinstance(command, list)
        assert command[:3] == ["/usr/bin/sh", "-c", '"$@" &\nchild=$!\nwait "$child"\n']
        assert str(command[3]).startswith("pynchy-runtime-")
        assert process["start_new_session"] is True


def test_execute_checks_the_wide_process_command_for_its_owned_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    commands: list[list[str]] = []
    temporal_marker = _process_marker("temporal")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": spec.namespace,
            "server_port": spec.server_port,
            "gateway_port": spec.gateway_port,
            "temporal_port": spec.temporal_port,
            "gateway_key": "sk-runtime-key",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
            "temporal_pid": 5678,
            "temporal_marker": temporal_marker,
        },
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:2] == ["/usr/bin/ps", "-ww"]:
            value = marker if command[3] == "1234" else temporal_marker
            return subprocess.CompletedProcess(command, 0, stdout=f"/bin/sh -c ... {value} ...")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/ps")
    monkeypatch.setattr(harness.subprocess, "run", run)

    assert harness.execute(spec.root, ["pytest"]) == 0
    assert commands[:2] == [
        ["/usr/bin/ps", "-ww", "-p", "1234", "-o", "command="],
        ["/usr/bin/ps", "-ww", "-p", "5678", "-o", "command="],
    ]


def test_run_exposes_runtime_state_to_command_and_cleans_up_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    state: dict[str, object] = {"gateway_key": "sk-runtime-key"}
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    stops: list[tuple[Path, bool]] = []

    def run_command(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        assert check is False
        calls.append((command, cwd, env))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(harness, "setup", lambda _spec: state)
    monkeypatch.setattr(harness.subprocess, "run", run_command)
    monkeypatch.setattr(
        harness,
        "stop",
        lambda root, *, preserve_state=False: stops.append((root, preserve_state)),
    )

    assert harness.run(spec, ["uv", "run", "pytest", "-m", "runtime"]) == 0

    assert calls[0][0] == ["uv", "run", "pytest", "-m", "runtime"]
    assert calls[0][1] == spec.root
    assert calls[0][2]["PYNCHY_RUNTIME_STATE"] == str(spec.state_path)
    assert calls[0][2]["PYNCHY_RUNTIME_URL"] == "http://127.0.0.1:18484"
    assert calls[0][2]["PYNCHY_RUNTIME_GATEWAY_URL"] == "http://127.0.0.1:14010"
    assert stops == [(spec.root, False)]


def test_run_stops_live_resources_but_preserves_diagnostics_on_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    state: dict[str, object] = {"gateway_key": "sk-runtime-key"}
    stops: list[tuple[Path, bool]] = []

    monkeypatch.setattr(harness, "setup", lambda _spec: state)
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1),
    )
    monkeypatch.setattr(
        harness,
        "stop",
        lambda root, *, preserve_state=False: stops.append((root, preserve_state)),
    )

    assert harness.run(spec, ["pytest", "-m", "runtime"]) == 1

    assert stops == [(spec.root, True)]
