"""Repository agent hooks reject imperative production infrastructure changes."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from scripts.agent_hooks.guard_infrastructure_as_code import (
    blocked_infrastructure_operation,
    run_hook,
)


@pytest.mark.parametrize(
    "command",
    [
        "sudo k3s ctr images import /tmp/bridge.tar",
        "docker image load --input /tmp/bridge.tar",
        "ssh server 'sudo k3s ctr images import /tmp/bridge.tar'",
        "kubectl --context production -n app patch deployment/app --patch '{}';",
        'ssh -o BatchMode=yes server "sudo k3s kubectl -n app edit deployment/app"',
        "kubectl -n app replace -f /tmp/deployment.yaml",
        "kubectl -n app scale deployment/app --replicas=2",
        "kubectl -n app set image deployment/app app=example.invalid/app:latest",
        "kubectl create secret generic provider --from-literal=value=secret",
    ],
)
def test_blocks_imperative_infrastructure_changes(command: str) -> None:
    assert blocked_infrastructure_operation(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "sudo k3s ctr images list",
        "sudo k3s kubectl -n app get pods",
        "ssh server 'sudo k3s kubectl -n app logs deploy/app --since=10m'",
        "ssh server 'sudo k3s kubectl -n app rollout status deployment/app'",
        "kubectl apply -k ops/k3s",
        "kubectl apply -f deploy/k3s/bootstrap/namespace.yaml",
        "kubectl create job release-manual --from=cronjob/release-monitor",
        "kubectl delete pod image-preflight",
        "rg -n 'kubectl patch' docs",
    ],
)
def test_allows_observation_diagnostics_and_declarative_apply(command: str) -> None:
    assert blocked_infrastructure_operation(command) is None


def test_hook_denies_codex_command_payload_without_echoing_command() -> None:
    command = "ssh server 'sudo k3s kubectl patch secret/provider --patch super-secret'"
    output = io.StringIO()

    assert (
        run_hook(
            io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})),
            output,
        )
        == 0
    )

    decision = json.loads(output.getvalue())["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "tracked infrastructure as code" in decision["permissionDecisionReason"]
    assert "super-secret" not in decision["permissionDecisionReason"]


def test_hook_accepts_nested_codex_payload_and_cmd_field() -> None:
    output = io.StringIO()

    run_hook(
        io.StringIO(
            json.dumps(
                {
                    "tool": {
                        "name": "Bash",
                        "input": {"cmd": "docker load --input /tmp/bridge.tar"},
                    }
                }
            )
        ),
        output,
    )

    assert json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_allows_non_shell_tools_and_safe_shell_commands() -> None:
    for payload in (
        {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
        {"tool_name": "Bash", "tool_input": {"command": "kubectl get pods"}},
    ):
        output = io.StringIO()
        run_hook(io.StringIO(json.dumps(payload)), output)
        assert not output.getvalue()


def test_repo_wires_shared_guard_into_codex_and_claude() -> None:
    codex = json.loads(Path(".codex/hooks.json").read_text(encoding="utf-8"))
    claude = json.loads(Path(".claude/settings.json").read_text(encoding="utf-8"))

    for document in (codex, claude):
        groups = document["hooks"]["PreToolUse"]
        handlers = [handler for group in groups for handler in group["hooks"]]
        commands = [handler["command"] for handler in handlers]
        assert any("guard_infrastructure_as_code.py" in command for command in commands)
