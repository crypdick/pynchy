"""Kubernetes container-CLI adapter behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.plugins.runtimes.kubernetes_runtime.cli import build_resources
from pynchy.plugins.runtimes.kubernetes_runtime.cli import run as run_cli
from pynchy.plugins.runtimes.kubernetes_runtime.runtime import KubernetesContainerRuntime

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_builds_agent_pod_from_existing_container_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "groups" / "admin"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    resources = build_resources(
        [
            "run",
            "--name",
            "pynchy-admin",
            "--label",
            "com.pynchy.role=agent",
            "--memory",
            "2048m",
            "-e",
            "OPENAI_API_KEY",
            "-v",
            f"{workspace}:/home/agent/workspace",
            "pynchy-agent:latest",
        ],
        shared_root=tmp_path,
        pvc_name="pynchy-data",
        namespace="pynchy",
    )

    pod = resources[0]
    container = pod["spec"]["containers"][0]
    assert pod["metadata"]["name"] == "pynchy-admin"
    assert pod["spec"]["volumes"] == [
        {"name": "shared", "persistentVolumeClaim": {"claimName": "pynchy-data"}}
    ]
    assert pod["spec"]["securityContext"]["fsGroup"] == 3000
    assert container["resources"]["limits"]["memory"] == "2048Mi"
    assert container["env"] == [{"name": "OPENAI_API_KEY", "value": "secret"}]
    assert container["volumeMounts"] == [
        {
            "name": "shared",
            "mountPath": "/home/agent/workspace",
            "subPath": "groups/admin",
        }
    ]
    assert workspace.stat().st_mode & 0o7777 == 0o2775


def test_read_only_mount_keeps_source_permissions(tmp_path: Path) -> None:
    workspace = tmp_path / "groups" / "admin"
    workspace.mkdir(parents=True, mode=0o750)

    build_resources(
        [
            "run",
            "--name",
            "pynchy-admin",
            "-v",
            f"{workspace}:/home/agent/workspace:ro",
            "pynchy-agent:latest",
        ],
        shared_root=tmp_path,
        pvc_name="pynchy-data",
        namespace="pynchy",
    )

    assert workspace.stat().st_mode & 0o7777 == 0o750


def test_writable_ipc_mount_makes_existing_subdirectories_group_writable(tmp_path: Path) -> None:
    ipc_root = tmp_path / "data" / "ipc" / "home"
    input_dir = ipc_root / "input"
    output_dir = ipc_root / "output"
    input_dir.mkdir(parents=True, mode=0o755)
    output_dir.mkdir(mode=0o755)

    build_resources(
        [
            "run",
            "--name",
            "pynchy-home",
            "-v",
            f"{ipc_root}:/run/pynchy",
            "pynchy-agent:latest",
        ],
        shared_root=tmp_path,
        pvc_name="pynchy-data",
        namespace="pynchy",
    )

    assert input_dir.stat().st_mode & 0o7777 == 0o2775
    assert output_dir.stat().st_mode & 0o7777 == 0o2775


def test_builds_detached_mcp_pod_and_service(
    tmp_path: Path,
) -> None:
    resources = build_resources(
        [
            "run",
            "-d",
            "--name",
            "pynchy-mcp-playwright",
            "-p",
            "19101:8931",
            "mcr.microsoft.com/playwright/mcp:latest",
            "--port",
            "8931",
        ],
        shared_root=tmp_path,
        pvc_name="pynchy-data",
        namespace="pynchy",
    )

    pod, service = resources
    assert pod["spec"]["restartPolicy"] == "Always"
    assert service["spec"]["ports"] == [{"name": "tcp-8931", "port": 8931, "targetPort": 8931}]
    assert service["metadata"]["name"] == "pynchy-mcp-playwright"


def test_rejects_mount_outside_shared_pvc(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside Kubernetes shared root"):
        build_resources(
            ["run", "--name", "pynchy-admin", "-v", "/etc:/host", "image"],
            shared_root=tmp_path,
            pvc_name="pynchy-data",
            namespace="pynchy",
        )


def test_runtime_probe_uses_namespace_scoped_permission() -> None:
    with (
        patch(
            "pynchy.plugins.runtimes.kubernetes_runtime.runtime.kubectl_command",
            return_value=["kubectl", "--namespace", "pynchy"],
        ),
        patch("subprocess.run", return_value=_ProcessResult(returncode=0)) as run,
    ):
        KubernetesContainerRuntime().ensure_running()

    command = run.call_args.args[0]
    assert command[3:5] == ["get", "pods"]
    assert "namespace" not in command


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{{.State.Running}}", "true\n"),
        ("{{.State.Status}}", "running\n"),
    ],
)
def test_inspect_matches_requested_docker_state_shape(
    template: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _ProcessResult(returncode=0, stdout="Running")
    with (
        patch(
            "pynchy.plugins.runtimes.kubernetes_runtime.cli.kubectl_command",
            return_value=["kubectl"],
        ),
        patch("subprocess.run", return_value=result),
    ):
        assert run_cli(["inspect", "-f", template, "pynchy-agent"]) == 0

    assert capsys.readouterr().out == expected
