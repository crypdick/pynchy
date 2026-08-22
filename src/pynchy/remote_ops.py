"""Fixed, read-only Kubernetes diagnostics for one private deployment target."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - fixed SSH and kubectl argv only.
from dataclasses import dataclass

_SSH_TIMEOUT_SECONDS = 30
_PYNCHY_CONTAINER = "pynchy"
_PYNCHY_DEPLOYMENT = "pynchy"
_PYNCHY_CLI = "/opt/pynchy/.venv/bin/pynchy"
_MESSAGES_QUERY = (
    "SELECT timestamp, chat_jid, sender_name, message_type, substr(content, 1, 160) "
    "FROM messages ORDER BY timestamp DESC LIMIT 20;"
)
_EVENTS_QUERY = (
    "SELECT timestamp, chat_jid, json_extract(payload, '$.tool_name') "
    "FROM events WHERE event_type = 'agent_trace' "
    "ORDER BY timestamp DESC LIMIT 20;"
)


class RemoteOpsError(RuntimeError):
    """A fixed remote diagnostic command could not return evidence."""


@dataclass(frozen=True)
class RemoteOpsTarget:
    """Validated private deployment target."""

    ssh_host: str
    namespace: str

    @classmethod
    def from_config(cls, config: object) -> RemoteOpsTarget:
        ssh_host = getattr(config, "ssh_host", None)
        namespace = getattr(config, "namespace", None)
        if not isinstance(ssh_host, str) or not isinstance(namespace, str):
            raise RemoteOpsError("Configure private [ops] ssh_host and namespace before using ops")
        return cls(ssh_host=ssh_host, namespace=namespace)


def _run(target: RemoteOpsTarget, command: tuple[str, ...]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed diagnostic argv plus validated config atoms.
        ["/usr/bin/ssh", target.ssh_host, "--", *command],
        check=False,
        capture_output=True,
        text=True,
        timeout=_SSH_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "remote command failed"
        raise RemoteOpsError(detail)
    return result.stdout.strip()


def _kubectl(target: RemoteOpsTarget, *arguments: str) -> str:
    return _run(target, ("sudo", "k3s", "kubectl", "-n", target.namespace, *arguments))


def _exec(target: RemoteOpsTarget, *arguments: str) -> str:
    return _kubectl(
        target,
        "exec",
        f"deploy/{_PYNCHY_DEPLOYMENT}",
        "-c",
        _PYNCHY_CONTAINER,
        "--",
        *arguments,
    )


def remote_status(target: RemoteOpsTarget) -> str:
    """Return bounded app status plus Kubernetes rollout evidence."""
    status = _exec(target, _PYNCHY_CLI, "status", "--summary")
    deployment = _kubectl(target, "get", "deployment", _PYNCHY_DEPLOYMENT, "-o", "json")
    observed, generation, ready, replicas, sha = _rollout_fields(deployment)
    rollout = "ready" if observed == generation and ready == replicas else "progressing"
    return f"{status}\nrollout: {rollout} ({ready}/{replicas}), release_sha: {sha}"


def _rollout_fields(deployment: str) -> tuple[object, object, int, int, object]:
    try:
        data = json.loads(deployment)
        metadata = data["metadata"]
        status = data["status"]
        return (
            status.get("observedGeneration"),
            metadata["generation"],
            status.get("readyReplicas", 0),
            data["spec"].get("replicas", 0),
            metadata.get("annotations", {}).get("pynchy.dev/release-sha", "unknown"),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RemoteOpsError(f"invalid deployment rollout response: {exc}") from exc


def remote_logs(target: RemoteOpsTarget) -> str:
    """Return a fixed tail of application logs."""
    return _kubectl(
        target,
        "logs",
        f"deploy/{_PYNCHY_DEPLOYMENT}",
        "-c",
        _PYNCHY_CONTAINER,
        "--tail=100",
        "--since=30m",
    )


def remote_messages(target: RemoteOpsTarget) -> str:
    """Return a fixed bounded recent-message projection."""
    return _exec(
        target, "sqlite3", "-readonly", "/srv/pynchy/app/data/messages.db", _MESSAGES_QUERY
    )


def remote_events(target: RemoteOpsTarget) -> str:
    """Return a fixed bounded recent-agent-event projection."""
    return _exec(target, "sqlite3", "-readonly", "/srv/pynchy/app/data/messages.db", _EVENTS_QUERY)


def run_remote_op(config: object, operation: str) -> str:
    """Run one named diagnostic; command and query shape stay repository-owned."""
    target = RemoteOpsTarget.from_config(config)
    operations = {
        "status": remote_status,
        "logs": remote_logs,
        "messages": remote_messages,
        "events": remote_events,
    }
    return operations[operation](target)
