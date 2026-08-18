"""Behavior checks for the K3s release monitor."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - test invokes a repository-owned script.
from pathlib import Path

_TARGET_SHA = "a" * 40


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_monitor(
    tmp_path: Path,
    *,
    current_sha: str | None = None,
    desktop_current_sha: str | None = None,
    fail_preflight: bool = False,
    fail_rollout: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    project_root = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "kubectl.log"
    patched = tmp_path / "patched"
    undone = tmp_path / "undone"
    _write_executable(
        fake_bin / "gh",
        '#!/bin/sh\nprintf "%s\\n" "$PYNCHY_TEST_TARGET_SHA"\n',
    )
    _write_executable(
        fake_bin / "kubectl",
        "#!/bin/sh\n"
        'while [ "$1" = "--kubeconfig" ] || [ "$1" = "-n" ]; do shift 2; done\n'
        'printf "%s\\n" "$*" >> "$PYNCHY_TEST_CALL_LOG"\n'
        'if [ "$1" = "get" ] && [ "$3" = "pynchy-desktop" ]; then\n'
        '  printf "%s" "$PYNCHY_TEST_DESKTOP_CURRENT_SHA"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "get" ] && [ "$2" = "deployment" ]; then\n'
        '  printf "%s" "$PYNCHY_TEST_CURRENT_SHA"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "exec" ]; then\n'
        '  if [ "$2" = "-i" ] && [ "$3" = "deployment/pynchy-desktop" ]; then\n'
        '    printf \'{"protocol_version":1,"supported_actions":[],"ready":true}\\n\'\n'
        "    exit 0\n"
        "  fi\n"
        '  if [ -e "$PYNCHY_TEST_PATCHED" ]; then\n'
        "    printf '"
        '{"service":{"status":"ok"},'
        '"deploy":{"head_sha":"%s","last_deploy_sha":"%s"},'
        '"gateway":{"ready":true},'
        '"temporal":{"cluster_healthy":true,"worker_running":true}}'
        '\\n\' "$PYNCHY_TEST_TARGET_SHA" "$PYNCHY_TEST_TARGET_SHA"\n'
        "  else\n"
        '    printf \'{"deploy":{"head_sha":"%s"}}\\n\' "$PYNCHY_TEST_TARGET_SHA"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "wait" ] && [ "$PYNCHY_TEST_FAIL_PREFLIGHT" = "1" ]; then\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "patch" ]; then touch "$PYNCHY_TEST_PATCHED"; exit 0; fi\n'
        'if [ "$1" = "rollout" ] && [ "$2" = "undo" ]; then\n'
        '  touch "$PYNCHY_TEST_UNDONE"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "rollout" ] && [ "$2" = "status" ] '
        '&& [ "$PYNCHY_TEST_FAIL_ROLLOUT" = "1" ] '
        '&& [ ! -e "$PYNCHY_TEST_UNDONE" ]; then\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    env = {
        **os.environ,
        "GH_TOKEN": "test-token",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYNCHY_RELEASE_AGENT_IMAGE": "registry.example/pynchy-agent",
        "PYNCHY_RELEASE_HOST_IMAGE": "registry.example/pynchy-host",
        "PYNCHY_RELEASE_REPOSITORY": "owner/pynchy",
        "PYNCHY_KUBECONFIG": str(tmp_path / "kubeconfig"),
        "PYNCHY_TEST_CALL_LOG": str(call_log),
        "PYNCHY_TEST_CURRENT_SHA": current_sha or "b" * 40,
        "PYNCHY_TEST_DESKTOP_CURRENT_SHA": desktop_current_sha or "b" * 40,
        "PYNCHY_TEST_FAIL_PREFLIGHT": "1" if fail_preflight else "0",
        "PYNCHY_TEST_FAIL_ROLLOUT": "1" if fail_rollout else "0",
        "PYNCHY_TEST_PATCHED": str(patched),
        "PYNCHY_TEST_TARGET_SHA": _TARGET_SHA,
        "PYNCHY_TEST_UNDONE": str(undone),
    }
    result = subprocess.run(  # noqa: S603 - executable is the repository-owned monitor.
        [str(project_root / "deploy" / "k3s" / "release-monitor.sh")],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    return result, calls


def test_monitor_releases_one_healthy_main_revision(tmp_path: Path) -> None:
    result, calls = _run_monitor(tmp_path)

    assert result.returncode == 0, result.stderr
    patch = next(call for call in calls if call.startswith("patch deployment pynchy --"))
    desktop_patch = next(
        call for call in calls if call.startswith("patch deployment pynchy-desktop")
    )
    assert f"registry.example/pynchy-host:{_TARGET_SHA}" in patch
    assert f"registry.example/pynchy-agent:{_TARGET_SHA}" in patch
    assert f"registry.example/pynchy-host:{_TARGET_SHA}" in desktop_patch
    assert _TARGET_SHA in patch
    assert any(call.startswith("rollout status deployment/pynchy-desktop") for call in calls)
    assert any(call.startswith("exec -i deployment/pynchy-desktop -c desktop") for call in calls)
    assert not any(call.startswith("rollout undo") for call in calls)


def test_monitor_keeps_current_release_when_preflight_fails(tmp_path: Path) -> None:
    result, calls = _run_monitor(tmp_path, fail_preflight=True)

    assert result.returncode != 0
    assert not any(call.startswith("patch deployment") for call in calls)


def test_monitor_repairs_desktop_release_drift(tmp_path: Path) -> None:
    result, calls = _run_monitor(
        tmp_path,
        current_sha=_TARGET_SHA,
        desktop_current_sha="b" * 40,
    )

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("patch deployment pynchy-desktop") for call in calls)
    assert not any(call.startswith("patch deployment pynchy --") for call in calls)


def test_monitor_rolls_back_when_rollout_fails(tmp_path: Path) -> None:
    result, calls = _run_monitor(tmp_path, fail_rollout=True)

    assert result.returncode != 0
    assert any(call.startswith("patch deployment pynchy") for call in calls)
    assert any(call.startswith("rollout undo deployment/pynchy") for call in calls)
    assert any(call.startswith("rollout undo deployment/pynchy-desktop") for call in calls)
