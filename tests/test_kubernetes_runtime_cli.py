"""Kubernetes container-CLI adapter behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.plugins.runtimes.kubernetes_runtime import cli as kubernetes_cli
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


def test_group_writable_session_home_does_not_require_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_home = tmp_path / "sessions" / "home" / ".claude"
    session_home.mkdir(parents=True)
    session_home.chmod(0o2775)

    def deny_chmod(self: Path, mode: int) -> None:
        raise PermissionError("Caller has group access but does not own the directory")

    monkeypatch.setattr(type(session_home), "chmod", deny_chmod)
    resources = build_resources(
        [
            "run",
            "--name",
            "pynchy-home",
            "--label",
            "com.pynchy.role=agent",
            "-v",
            f"{session_home}:/home/agent/.claude",
            "pynchy-agent:latest",
        ],
        shared_root=tmp_path,
        pvc_name="pynchy-data",
        namespace="pynchy",
    )

    assert resources[0]["spec"]["containers"][0]["volumeMounts"][0]["subPath"] == (
        "sessions/home/.claude"
    )


def test_agent_pod_takes_ownership_of_migrated_session_homes(tmp_path: Path) -> None:
    claude_home = tmp_path / "data" / "sessions" / "home" / ".claude"
    codex_home = tmp_path / "data" / "sessions" / "home" / ".codex"
    claude_home.mkdir(parents=True)
    codex_home.mkdir()

    resources = build_resources(
        [
            "run",
            "--name",
            "pynchy-home",
            "--label",
            "com.pynchy.role=agent",
            "-v",
            f"{claude_home}:/home/agent/.claude",
            "-v",
            f"{codex_home}:/home/agent/.codex",
            "pynchy-agent:latest",
        ],
        shared_root=tmp_path,
        pvc_name="pynchy-data",
        namespace="pynchy",
    )

    init = resources[0]["spec"]["initContainers"][0]
    assert init["args"] == ["chown -R 3000:3000 /home/agent/.claude /home/agent/.codex"]
    assert init["securityContext"]["capabilities"] == {
        "drop": ["ALL"],
        "add": ["CHOWN", "DAC_READ_SEARCH"],
    }
    assert init["volumeMounts"] == [
        {
            "name": "shared",
            "mountPath": "/home/agent/.claude",
            "subPath": "data/sessions/home/.claude",
        },
        {
            "name": "shared",
            "mountPath": "/home/agent/.codex",
            "subPath": "data/sessions/home/.codex",
        },
    ]


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


def test_routes_vault_mounts_to_dedicated_pvc(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared"
    vault_root = shared_root / "vault"
    workspace = shared_root / "groups" / "admin"
    automation_memory = vault_root / "wiki" / "systems" / "pynchy" / "automation-memory"
    workspace.mkdir(parents=True)
    automation_memory.mkdir(parents=True)
    vault_root.chmod(0o750)

    resources = build_resources(
        [
            "run",
            "--name",
            "pynchy-admin",
            "-v",
            f"{vault_root}:/home/agent/memory",
            "-v",
            f"{automation_memory}:/home/agent/automation-memory",
            "-v",
            f"{workspace}:/home/agent/workspace",
            "pynchy-agent:latest",
        ],
        shared_root=shared_root,
        pvc_name="pynchy-data",
        vault_pvc_name="pynchy-vault",
        namespace="pynchy",
    )

    pod = resources[0]
    assert pod["spec"]["containers"][0]["volumeMounts"] == [
        {"name": "vault", "mountPath": "/home/agent/memory"},
        {
            "name": "vault",
            "mountPath": "/home/agent/automation-memory",
            "subPath": "wiki/systems/pynchy/automation-memory",
        },
        {
            "name": "shared",
            "mountPath": "/home/agent/workspace",
            "subPath": "groups/admin",
        },
    ]
    assert pod["spec"]["volumes"] == [
        {"name": "shared", "persistentVolumeClaim": {"claimName": "pynchy-data"}},
        {"name": "vault", "persistentVolumeClaim": {"claimName": "pynchy-vault"}},
    ]
    assert vault_root.stat().st_mode & 0o7777 == 0o750


def test_runtime_settings_and_name_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYNCHY_KUBERNETES_NAMESPACE", "agents")
    monkeypatch.setenv("PYNCHY_KUBERNETES_PVC", "shared")
    monkeypatch.setenv("PYNCHY_KUBERNETES_VAULT_PVC", "vault")
    monkeypatch.setenv("PYNCHY_KUBERNETES_SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("PYNCHY_KUBERNETES_PULL_POLICY", "Always")

    assert kubernetes_cli.runtime_settings() == kubernetes_cli.RuntimeSettings(
        namespace="agents",
        pvc_name="shared",
        vault_pvc_name="vault",
        shared_root=tmp_path,
        pull_policy="Always",
    )
    assert kubernetes_cli.pod_name("!!!") == "pynchy"
    long_name = "PYNCHY_" + "agent-" * 20
    assert len(kubernetes_cli.pod_name(long_name)) == 63
    assert kubernetes_cli.pod_name(long_name) == kubernetes_cli.pod_name(long_name)


def test_builds_all_supported_run_options(
    tmp_path: Path,
) -> None:
    relative = tmp_path / "relative"
    relative.mkdir()

    resources = build_resources(
        [
            "run",
            "--init",
            "--network",
            "none",
            "--restart",
            "no",
            "--add-host",
            "host:127.0.0.1",
            "--name",
            "pynchy-options",
            "--label",
            "ignored-label",
            "--memory",
            "1Gi",
            "-e",
            "INLINE=value",
            "-v",
            "cache:/cache",
            "-v",
            ".:/shared:ro",
            "--mount",
            "type=bind,source=relative,target=/data,readonly,ignored",
            "-p",
            "127.0.0.1:8080:8080/tcp",
            "-p",
            "8080",
            "image",
            "serve",
        ],
        shared_root=tmp_path,
        pvc_name="shared",
        namespace="agents",
        pull_policy="Always",
    )

    pod, service = resources
    container = pod["spec"]["containers"][0]
    assert container["args"] == ["serve"]
    assert container["env"] == [{"name": "INLINE", "value": "value"}]
    assert container["resources"]["limits"]["memory"] == "1Gi"
    assert container["ports"] == [{"containerPort": 8080}]
    assert container["imagePullPolicy"] == "Always"
    assert container["volumeMounts"][1] == {
        "name": "shared",
        "mountPath": "/shared",
        "readOnly": True,
    }
    assert container["volumeMounts"][2]["readOnly"] is True
    assert service["spec"]["ports"] == [{"name": "tcp-8080", "port": 8080, "targetPort": 8080}]
    assert (tmp_path / ".runtime" / "volumes" / "cache").is_dir()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["run", "--name"], "Missing value"),
        (["run", "--wat", "image"], "Unsupported container run option"),
        (["run", "image"], "requires --name and image"),
        (["run", "--name", "pod"], "requires --name and image"),
        (["run", "--name", "pod", "-v", "bad", "image"], "Invalid volume"),
        (
            ["run", "--name", "pod", "--mount", "type=volume,readonly", "image"],
            "Unsupported mount",
        ),
        (["run", "--name", "pod", "-p", "0", "image"], "Invalid container port"),
    ],
)
def test_rejects_invalid_run_arguments(
    tmp_path: Path,
    args: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_resources(args, shared_root=tmp_path, pvc_name="shared", namespace="agents")


def test_kubectl_command_writes_private_in_cluster_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config" / "kubeconfig.json"
    service_account = tmp_path / "service-account"
    monkeypatch.setenv("PYNCHY_KUBERNETES_NAMESPACE", "agents")
    with (
        patch.object(kubernetes_cli, "_KUBECONFIG_PATH", config_path),
        patch.object(kubernetes_cli, "_SERVICE_ACCOUNT_ROOT", service_account),
    ):
        assert kubernetes_cli.kubectl_command() == [
            "kubectl",
            "--kubeconfig",
            str(config_path),
            "--namespace",
            "agents",
        ]

    config = json.loads(config_path.read_text())
    assert config["clusters"][0]["cluster"]["certificate-authority"] == str(
        service_account / "ca.crt"
    )
    assert config["users"][0]["user"]["tokenFile"] == str(service_account / "token")
    assert config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("detached", "apply_code", "expected", "calls"),
    [
        (False, 0, 4, 2),
        (True, 0, 0, 1),
        (False, 3, 3, 1),
    ],
)
def test_run_applies_resources_and_streams_attached_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    detached: bool,
    apply_code: int,
    expected: int,
    calls: int,
) -> None:
    monkeypatch.setenv("PYNCHY_KUBERNETES_SHARED_ROOT", str(tmp_path))
    results = [
        _ProcessResult(returncode=apply_code, stdout="applied\n", stderr="warning\n"),
        _ProcessResult(returncode=4),
    ]
    args = ["run", *(["-d"] if detached else []), "--name", "pod", "image"]
    with (
        patch.object(kubernetes_cli, "kubectl_command", return_value=["kubectl"]),
        patch("subprocess.run", side_effect=results) as run,
    ):
        assert run_cli(args) == expected

    assert run.call_count == calls
    captured = capsys.readouterr()
    assert captured.out == "applied\n"
    assert captured.err == "warning\n"


@pytest.mark.parametrize(
    ("args", "expected_call"),
    [
        (["logs", "pod"], ("logs", "pod")),
        (["logs", "--tail", "20", "pod"], ("logs", "--tail=20", "pod")),
    ],
)
def test_logs_translate_tail_option(args: list[str], expected_call: tuple[str, ...]) -> None:
    with patch.object(kubernetes_cli, "_kubectl", return_value=6) as kubectl:
        assert run_cli(args) == 6
    kubectl.assert_called_once_with(*expected_call)


@pytest.mark.parametrize(
    ("args", "results", "expected", "grace_period"),
    [
        (["stop", "-t", "9", "pod"], [7, 0], 7, "9"),
        (["stop", "pod"], [0, 0], 0, "5"),
        (["rm", "pod"], [0, 8], 8, "0"),
    ],
)
def test_delete_operations(
    args: list[str],
    results: list[int],
    expected: int,
    grace_period: str,
) -> None:
    with patch.object(kubernetes_cli, "_kubectl", side_effect=results) as kubectl:
        assert run_cli(args) == expected
    service_delete, pod_delete = (call.args for call in kubectl.call_args_list)
    assert "--wait=false" in service_delete
    assert f"--grace-period={grace_period}" in pod_delete
    assert "--wait=false" in pod_delete
    assert ("--force" in pod_delete) is (args[0] == "rm")


@pytest.mark.parametrize("command", ["image", "pull", "network"])
def test_noop_operations_succeed(command: str) -> None:
    assert run_cli([command]) == 0


def test_empty_and_unsupported_operations_fail(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli([]) == 2
    assert run_cli(["exec"]) == 2
    assert "Unsupported Kubernetes container operation: exec" in capsys.readouterr().err


def test_main_exits_with_cli_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kubernetes_cli.sys, "argv", ["runtime", "image"])
    with pytest.raises(SystemExit, match="0"):
        kubernetes_cli.main()


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


def test_runtime_probe_keeps_pending_agent_pod_alive() -> None:
    pods = {
        "items": [
            {
                "metadata": {"annotations": {"pynchy.dev/runtime-name": "pynchy-home"}},
                "status": {"phase": "Pending"},
            }
        ]
    }
    result = _ProcessResult(returncode=0, stdout=json.dumps(pods))
    with (
        patch(
            "pynchy.plugins.runtimes.kubernetes_runtime.runtime.kubectl_command",
            return_value=["kubectl"],
        ),
        patch("subprocess.run", return_value=result),
    ):
        running = KubernetesContainerRuntime().list_running_containers(prefix="pynchy-home")

    assert running == ["pynchy-home"]


def test_runtime_inventory_filters_phase_and_prefix() -> None:
    def pod(name: str = "", phase: str = "") -> dict[str, object]:
        return {
            "metadata": {"annotations": {"pynchy.dev/runtime-name": name}},
            "status": {"phase": phase},
        }

    pods = {
        "items": [
            pod("pynchy-running", "Running"),
            pod("other-pending", "Pending"),
            pod("pynchy-stopped", "Succeeded"),
            {},
        ]
    }
    with (
        patch(
            "pynchy.plugins.runtimes.kubernetes_runtime.runtime.kubectl_command",
            return_value=["kubectl"],
        ),
        patch("subprocess.run", return_value=_ProcessResult(0, json.dumps(pods))),
    ):
        assert KubernetesContainerRuntime().list_running_containers() == ["pynchy-running"]


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        ([None], False),
        (["/usr/bin/kubectl", None], False),
        (["/usr/bin/kubectl", "/usr/bin/pynchy-kubernetes-runtime"], True),
    ],
)
def test_runtime_availability(found: list[str | None], *, expected: bool) -> None:
    with patch("shutil.which", side_effect=found):
        assert KubernetesContainerRuntime().is_available() is expected


@pytest.mark.parametrize(
    ("force", "returncode", "expected", "grace_period"),
    [(True, 0, True, "0"), (False, 1, False, "5")],
)
def test_runtime_removes_container(
    *,
    force: bool,
    returncode: int,
    expected: bool,
    grace_period: str,
) -> None:
    with (
        patch(
            "pynchy.plugins.runtimes.kubernetes_runtime.runtime.kubectl_command",
            return_value=["kubectl"],
        ),
        patch("subprocess.run", return_value=_ProcessResult(returncode)) as run,
    ):
        assert KubernetesContainerRuntime().remove_container("POD", force=force) is expected
    command = run.call_args.args[0]
    assert f"--grace-period={grace_period}" in command
    assert "--wait=false" in command
    assert ("--force" in command) is force


@pytest.mark.parametrize(
    ("phase", "template", "expected"),
    [
        ("Pending", "{{.State.Running}}", "true\n"),
        ("Running", "{{.State.Running}}", "true\n"),
        ("Succeeded", "{{.State.Running}}", "false\n"),
        ("Failed", "{{.State.Running}}", "false\n"),
        ("Running", "{{.State.Status}}", "running\n"),
    ],
)
def test_inspect_matches_requested_docker_state_shape(
    phase: str,
    template: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _ProcessResult(returncode=0, stdout=phase)
    with (
        patch(
            "pynchy.plugins.runtimes.kubernetes_runtime.cli.kubectl_command",
            return_value=["kubectl"],
        ),
        patch("subprocess.run", return_value=result),
    ):
        assert run_cli(["inspect", "-f", template, "pynchy-agent"]) == 0

    assert capsys.readouterr().out == expected


def test_inspect_without_format_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(kubernetes_cli, "kubectl_command", return_value=["kubectl"]),
        patch("subprocess.run", return_value=_ProcessResult(5, "Pending")),
    ):
        assert run_cli(["inspect", "pod"]) == 5
    assert not capsys.readouterr().out
