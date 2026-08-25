"""Public behavior tests for fixed remote operator diagnostics."""

from __future__ import annotations

import shlex
from subprocess import CompletedProcess  # noqa: S404 - synthetic subprocess results only.
from unittest.mock import patch

import pytest

from pynchy.config.api import OpsConfig
from pynchy.remote_ops import RemoteOpsError, run_remote_op


def test_ops_status_uses_only_fixed_ssh_and_kubernetes_commands() -> None:
    responses = [
        CompletedProcess(
            [], 0, '{"deploy":{"head_sha":"abc"},"queue":{},"service":{"status":"ok"}}', ""
        ),
        CompletedProcess(
            [],
            0,
            '{"metadata":{"generation":2},"spec":{"replicas":1,"template":{"metadata":{"annotations":{"pynchy.dev/release-sha":"abc"}},"spec":{"containers":[{"name":"pynchy","image":"registry.example/pynchy:abc"}]}}},"status":{"observedGeneration":2,"readyReplicas":1,"updatedReplicas":1,"availableReplicas":1}}',
            "",
        ),
    ]
    with patch("pynchy.remote_ops.subprocess.run", side_effect=responses) as run:
        output = run_remote_op(OpsConfig(ssh_host="ops-host", namespace="pynchy"), "status")

    assert "rollout: ready (1/1), release_sha: abc, image: registry.example/pynchy:abc" in output
    assert run.call_args_list[0].args[0] == [
        "/usr/bin/ssh",
        "ops-host",
        "--",
        (
            "sudo k3s kubectl -n pynchy exec deploy/pynchy -c pynchy -- "
            "/opt/pynchy/.venv/bin/pynchy status --summary"
        ),
    ]


@pytest.mark.parametrize(
    ("operation", "expected_tail"),
    [
        (
            "logs",
            ["logs", "deploy/pynchy", "-c", "pynchy", "--tail=100", "--since=30m"],
        ),
        (
            "messages",
            [
                "exec",
                "deploy/pynchy",
                "-c",
                "pynchy",
                "--",
                "sqlite3",
                "-readonly",
                "/srv/pynchy/app/data/messages.db",
                (
                    "SELECT timestamp, chat_jid, sender_name, message_type, "
                    "substr(content, 1, 160) FROM messages "
                    "ORDER BY timestamp DESC LIMIT 20;"
                ),
            ],
        ),
        (
            "events",
            [
                "exec",
                "deploy/pynchy",
                "-c",
                "pynchy",
                "--",
                "sqlite3",
                "-readonly",
                "/srv/pynchy/app/data/messages.db",
                (
                    "SELECT timestamp, chat_jid, json_extract(payload, '$.tool_name') "
                    "FROM events WHERE event_type = 'agent_trace' "
                    "ORDER BY timestamp DESC LIMIT 20;"
                ),
            ],
        ),
    ],
)
def test_ops_read_commands_have_no_caller_controlled_command_shape(
    operation: str, expected_tail: list[str]
) -> None:
    with patch(
        "pynchy.remote_ops.subprocess.run", return_value=CompletedProcess([], 0, "ok", "")
    ) as run:
        assert run_remote_op(OpsConfig(ssh_host="ops-host", namespace="pynchy"), operation) == "ok"

    command = run.call_args.args[0]
    assert command[:3] == ["/usr/bin/ssh", "ops-host", "--"]
    assert shlex.split(command[3]) == ["sudo", "k3s", "kubectl", "-n", "pynchy", *expected_tail]


def test_ops_requires_complete_private_target() -> None:
    with pytest.raises(RemoteOpsError, match=r"private \[ops\]"):
        run_remote_op(OpsConfig(), "logs")


def test_ops_reports_remote_command_failure() -> None:
    with (
        patch(
            "pynchy.remote_ops.subprocess.run",
            return_value=CompletedProcess([], 1, "", "permission denied"),
        ),
        pytest.raises(RemoteOpsError, match="permission denied"),
    ):
        run_remote_op(OpsConfig(ssh_host="ops-host", namespace="pynchy"), "logs")


def test_ops_rejects_invalid_deployment_response() -> None:
    responses = [
        CompletedProcess([], 0, '{"service":{"status":"ok"}}', ""),
        CompletedProcess([], 0, "{}", ""),
    ]
    with (
        patch("pynchy.remote_ops.subprocess.run", side_effect=responses),
        pytest.raises(RemoteOpsError, match="invalid deployment rollout response"),
    ):
        run_remote_op(OpsConfig(ssh_host="ops-host", namespace="pynchy"), "status")


def test_ops_status_does_not_report_old_ready_replicas_as_current() -> None:
    responses = [
        CompletedProcess([], 0, '{"service":{"status":"ok"}}', ""),
        CompletedProcess(
            [],
            0,
            '{"metadata":{"generation":2},"spec":{"replicas":1,"template":{"metadata":{},"spec":{"containers":[{"name":"sidecar","image":"other"},{"name":"pynchy","image":"registry.example/pynchy:next"}]}}},"status":{"observedGeneration":2,"readyReplicas":1,"availableReplicas":1}}',
            "",
        ),
    ]
    with patch("pynchy.remote_ops.subprocess.run", side_effect=responses):
        output = run_remote_op(OpsConfig(ssh_host="ops-host", namespace="pynchy"), "status")

    assert "rollout: progressing (1/1), release_sha: unknown" in output
