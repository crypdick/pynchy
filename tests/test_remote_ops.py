"""Public behavior tests for fixed remote operator diagnostics."""

from __future__ import annotations

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
            '{"metadata":{"generation":2,"annotations":{"pynchy.dev/release-sha":"abc"}},"spec":{"replicas":1},"status":{"observedGeneration":2,"readyReplicas":1}}',
            "",
        ),
    ]
    with patch("pynchy.remote_ops.subprocess.run", side_effect=responses) as run:
        output = run_remote_op(OpsConfig(ssh_host="dcloud", namespace="pynchy"), "status")

    assert "rollout: ready (1/1), release_sha: abc" in output
    assert run.call_args_list[0].args[0] == [
        "/usr/bin/ssh",
        "dcloud",
        "--",
        "sudo",
        "k3s",
        "kubectl",
        "-n",
        "pynchy",
        "exec",
        "deploy/pynchy",
        "-c",
        "pynchy",
        "--",
        "/opt/pynchy/.venv/bin/pynchy",
        "status",
        "--summary",
    ]


@pytest.mark.parametrize("operation", ["logs", "messages", "events"])
def test_ops_read_commands_have_no_caller_controlled_command_shape(operation: str) -> None:
    with patch(
        "pynchy.remote_ops.subprocess.run", return_value=CompletedProcess([], 0, "ok", "")
    ) as run:
        assert run_remote_op(OpsConfig(ssh_host="dcloud", namespace="pynchy"), operation) == "ok"

    command = run.call_args.args[0]
    assert command[:7] == ["/usr/bin/ssh", "dcloud", "--", "sudo", "k3s", "kubectl", "-n"]
    assert "rm" not in command


def test_ops_requires_complete_private_target() -> None:
    with pytest.raises(RemoteOpsError, match=r"private \[ops\]"):
        run_remote_op(OpsConfig(), "logs")
