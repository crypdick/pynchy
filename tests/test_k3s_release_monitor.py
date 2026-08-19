"""Behavior checks for the K3s release monitor."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - test invokes a repository-owned script.
from pathlib import Path

_TARGET_SHA = "a" * 40
_CURRENT_SHA = "b" * 40


def test_monitor_rolls_out_temporal_deployment() -> None:
    script = Path("deploy/k3s/application/release-monitor.sh").read_text(encoding="utf-8")

    assert "rollout status deployment/pynchy-temporal" in script
    assert "rollout status statefulset/pynchy-temporal" not in script


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_monitor(
    tmp_path: Path,
    *,
    current_sha: str = _CURRENT_SHA,
    desktop_sha: str | None = None,
    monitor_sha: str | None = None,
    fail_rollout: bool = False,
    image_pending: bool = False,
    no_successful_release: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    project_root = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    checkout = tmp_path / "checkout"
    (checkout / "deploy" / "k3s" / "application").mkdir(parents=True)
    (checkout / "deploy" / "k3s" / "application" / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n",
        encoding="utf-8",
    )
    gh_log = tmp_path / "gh.log"
    kubectl_log = tmp_path / "kubectl.log"
    git_log = tmp_path / "git.log"
    apply_count = tmp_path / "apply-count"
    release_patch = tmp_path / "vault.yaml"
    release_patch.write_text("apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8")
    _write_executable(
        fake_bin / "gh",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$PYNCHY_TEST_GH_LOG"\n'
        'if [ "$1" = "api" ] && [ "$PYNCHY_TEST_NO_RELEASE" != "1" ]; then\n'
        '  printf "%s\\n" "$PYNCHY_TEST_TARGET_SHA"\n'
        "fi\n",
    )
    _write_executable(
        fake_bin / "git",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$PYNCHY_TEST_GIT_LOG"\n'
        'if [ "$3" = "rev-parse" ] && [ "$4" = "HEAD" ]; then\n'
        '  if [ -e "$PYNCHY_TEST_GIT_ADVANCED" ]; then\n'
        '    printf "%s\\n" "$PYNCHY_TEST_TARGET_SHA"\n'
        "  else\n"
        '    printf "%s\\n" "$PYNCHY_TEST_CURRENT_SHA"\n'
        "  fi\n"
        "fi\n"
        'if [ "$3" = "merge" ]; then touch "$PYNCHY_TEST_GIT_ADVANCED"; fi\n'
        'if [ "$3" = "reset" ]; then rm -f "$PYNCHY_TEST_GIT_ADVANCED"; fi\n'
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "kubectl",
        "#!/bin/sh\n"
        'while [ "$1" = "--kubeconfig" ] || [ "$1" = "-n" ]; do shift 2; done\n'
        'printf "%s\\n" "$*" >> "$PYNCHY_TEST_KUBECTL_LOG"\n'
        'if [ "$1" = "get" ] && [ "$2" = "deployment" ]; then\n'
        '  if [ "$3" = "pynchy-desktop" ]; then\n'
        '    printf "%s" "$PYNCHY_TEST_DESKTOP_SHA"\n'
        "  else\n"
        '    printf "%s" "$PYNCHY_TEST_CURRENT_SHA"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "get" ] && [ "$2" = "cronjob" ]; then\n'
        '  printf "%s" "$PYNCHY_TEST_MONITOR_SHA"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "get" ] && [ "$2" = "pod" ]; then\n'
        "  printf '"
        '{"status":{"containerStatuses":['
        '{"state":{"waiting":{"reason":"ImagePullBackOff"}}}'
        "]}}\\n'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "wait" ] && [ "$PYNCHY_TEST_IMAGE_PENDING" = "1" ]; then\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "kustomize" ]; then\n'
        '  if grep -q \'": "/\' "$2/kustomization.yaml"; then exit 1; fi\n'
        "  printf 'apiVersion: v1\\nkind: Service\\n"
        "metadata:\\n  name: pynchy\\n  namespace: pynchy\\n'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "apply" ]; then\n'
        '  count=0; [ -e "$PYNCHY_TEST_APPLY_COUNT" ] && count=$(cat "$PYNCHY_TEST_APPLY_COUNT")\n'
        '  count=$((count + 1)); printf "%s" "$count" > "$PYNCHY_TEST_APPLY_COUNT"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "rollout" ] && [ "$2" = "status" ] '
        '&& [ "$PYNCHY_TEST_FAIL_ROLLOUT" = "1" ] '
        '&& [ "$(cat "$PYNCHY_TEST_APPLY_COUNT" 2>/dev/null)" = "1" ]; then\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "exec" ]; then\n'
        '  if [ "$2" = "-i" ]; then\n'
        '    printf \'{"protocol_version":1,"supported_actions":[],"ready":true}\\n\'\n'
        '  elif printf "%s" "$*" | grep -q "pynchy doctor --json"; then\n'
        "    printf '"
        '{"workspaces":[{"capabilities":['
        '{"id":"desktop.computer.use","status":"not_established"}'
        "]}]}\\n'\n"
        "  else\n"
        "    printf '"
        '{"service":{"status":"ok"},'
        '"deploy":{"head_sha":"%s","last_deploy_sha":"%s"},'
        '"gateway":{"ready":true},'
        '"temporal":{"cluster_healthy":true,"worker_running":true}}'
        '\\n\' "$PYNCHY_TEST_TARGET_SHA" "$PYNCHY_TEST_TARGET_SHA"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    env = {
        **os.environ,
        "GH_TOKEN": "test-token",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYNCHY_RELEASE_AGENT_IMAGE": "registry.example/pynchy-agent",
        "PYNCHY_RELEASE_CHECKOUT": str(checkout),
        "PYNCHY_RELEASE_HOST_IMAGE": "registry.example/pynchy-host",
        "PYNCHY_RELEASE_PATCH": str(release_patch),
        "PYNCHY_RELEASE_REPOSITORY": "owner/pynchy",
        "PYNCHY_KUBECONFIG": str(tmp_path / "kubeconfig"),
        "PYNCHY_TEST_APPLY_COUNT": str(apply_count),
        "PYNCHY_TEST_CURRENT_SHA": current_sha,
        "PYNCHY_TEST_DESKTOP_SHA": desktop_sha or current_sha,
        "PYNCHY_TEST_FAIL_ROLLOUT": "1" if fail_rollout else "0",
        "PYNCHY_TEST_GH_LOG": str(gh_log),
        "PYNCHY_TEST_GIT_ADVANCED": str(tmp_path / "git-advanced"),
        "PYNCHY_TEST_GIT_LOG": str(git_log),
        "PYNCHY_TEST_IMAGE_PENDING": "1" if image_pending else "0",
        "PYNCHY_TEST_KUBECTL_LOG": str(kubectl_log),
        "PYNCHY_TEST_MONITOR_SHA": monitor_sha or current_sha,
        "PYNCHY_TEST_NO_RELEASE": "1" if no_successful_release else "0",
        "PYNCHY_TEST_TARGET_SHA": _TARGET_SHA,
    }
    result = subprocess.run(  # noqa: S603 - executable is the repository-owned monitor.
        [str(project_root / "deploy" / "k3s" / "application" / "release-monitor.sh")],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    kubectl_calls = (
        kubectl_log.read_text(encoding="utf-8").splitlines() if kubectl_log.exists() else []
    )
    git_calls = git_log.read_text(encoding="utf-8").splitlines() if git_log.exists() else []
    return result, kubectl_calls, git_calls


def test_monitor_applies_one_healthy_published_revision(tmp_path: Path) -> None:
    result, kubectl_calls, git_calls = _run_monitor(tmp_path)

    assert result.returncode == 0, result.stderr
    gh_calls = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "actions/workflows/test.yml/runs" in gh_calls
    assert "status=success" in gh_calls
    assert any(call.startswith("apply -f ") for call in kubectl_calls)
    assert not any(call.startswith("patch deployment") for call in kubectl_calls)
    assert any("pynchy doctor --json" in call for call in kubectl_calls)
    assert any(
        call.endswith("fetch --quiet origin +refs/heads/main:refs/remotes/origin/main")
        for call in git_calls
    )
    assert any(call.endswith(f"merge --ff-only --quiet {_TARGET_SHA}") for call in git_calls)
    assert f"Released {_TARGET_SHA}." in result.stdout


def test_monitor_defers_missing_release_images_without_failure(tmp_path: Path) -> None:
    result, kubectl_calls, git_calls = _run_monitor(tmp_path, image_pending=True)

    assert result.returncode == 0, result.stderr
    assert "Waiting for release image" in result.stdout
    assert not any(call.startswith("apply -f ") for call in kubectl_calls)
    assert not any("merge --ff-only" in call for call in git_calls)


def test_monitor_noops_when_application_and_monitor_are_current(tmp_path: Path) -> None:
    result, kubectl_calls, git_calls = _run_monitor(
        tmp_path,
        current_sha=_TARGET_SHA,
        desktop_sha=_TARGET_SHA,
        monitor_sha=_TARGET_SHA,
    )

    assert result.returncode == 0, result.stderr
    assert f"Release {_TARGET_SHA} already active." in result.stdout
    assert not any(call.startswith("run ") for call in kubectl_calls)
    assert git_calls == [
        f"-C {tmp_path / 'checkout'} fetch --quiet origin +refs/heads/main:refs/remotes/origin/main"
    ]


def test_monitor_repairs_desktop_or_monitor_drift(tmp_path: Path) -> None:
    result, kubectl_calls, git_calls = _run_monitor(
        tmp_path,
        current_sha=_TARGET_SHA,
        desktop_sha=_CURRENT_SHA,
        monitor_sha=_CURRENT_SHA,
    )

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("apply -f ") for call in kubectl_calls)
    assert not any("merge --ff-only" in call for call in git_calls)


def test_monitor_reapplies_previous_application_when_rollout_fails(tmp_path: Path) -> None:
    result, kubectl_calls, git_calls = _run_monitor(tmp_path, fail_rollout=True)

    assert result.returncode != 0
    assert sum(call.startswith("apply -f ") for call in kubectl_calls) == 2
    assert any(call.endswith(f"reset --keep {_CURRENT_SHA}") for call in git_calls)
    assert not any(call.startswith("rollout undo") for call in kubectl_calls)


def test_monitor_waits_when_no_successful_release_exists(tmp_path: Path) -> None:
    result, kubectl_calls, git_calls = _run_monitor(tmp_path, no_successful_release=True)

    assert result.returncode == 0, result.stderr
    assert "No successful main release is published yet." in result.stdout
    assert kubectl_calls == []
    assert git_calls == []
