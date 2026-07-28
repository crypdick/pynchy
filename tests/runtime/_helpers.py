"""Black-box helpers shared by deterministic runtime integration tests."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess  # noqa: S404 - helper reads only a harness-owned Docker sidecar.
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

_POLL_INTERVAL_SECONDS = 0.2
_DEFAULT_TIMEOUT_SECONDS = 60.0
_RUNTIME_HISTORY_LIMIT = 10_000
_RESPONSE_REQUESTS_PROBE = (
    "from urllib.request import urlopen; "
    "url = 'http://127.0.0.1:8080' + '/' + '__pynchy_runtime__' + '/response-requests'; "
    "print(urlopen(url, timeout=5).read().decode())"
)


def runtime_state(state_path: Path | None = None) -> dict[str, Any]:
    """Load the runtime-owned state file, including its ephemeral test key."""
    resolved_path = state_path or _state_path_from_environment()
    value = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        pytest.fail(f"Runtime state must be a JSON object: {resolved_path}")
    return value


def json_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Make a loopback JSON request to the harness-owned HTTP surface."""
    request = Request(  # noqa: S310 - tests receive only harness-owned loopback URLs.
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers or {},
        method=method,
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - see Request above.
        return json.loads(response.read())


def groups(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return registered workspaces from the harness-owned database."""
    with sqlite3.connect(_database_path(state), timeout=10) as database:
        rows = database.execute(
            "SELECT jid, name, folder FROM registered_groups ORDER BY folder"
        ).fetchall()
    return [{"jid": jid, "name": name, "folder": folder} for jid, name, folder in rows]


def messages(
    state: dict[str, Any], jid: str, *, limit: int = _RUNTIME_HISTORY_LIMIT
) -> list[dict[str, Any]]:
    """Read enough history that response-count assertions cannot hit a moving cap."""
    with sqlite3.connect(_database_path(state), timeout=10) as database:
        rows = database.execute(
            """
            SELECT sender_name, content, timestamp, is_from_me
            FROM messages
            WHERE chat_jid = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (jid, limit),
        ).fetchall()
    return [
        {
            "sender_name": sender_name,
            "content": content,
            "timestamp": timestamp,
            "is_from_me": bool(is_from_me),
        }
        for sender_name, content, timestamp, is_from_me in reversed(rows)
    ]


def send_message(state: dict[str, Any], jid: str, content: str) -> None:
    """Submit one message through the explicit harness-only ingress."""
    value = json_request(
        f"{_required_string(state, 'server_url')}/__pynchy_runtime__/messages",
        method="POST",
        body={"jid": jid, "content": content},
        headers={"Content-Type": "application/json"},
    )
    if value != {"status": "accepted"}:
        pytest.fail(f"Runtime harness ingress returned unexpected payload: {value!r}")


def status(state: dict[str, Any]) -> dict[str, Any]:
    """Read the semantically meaningful public runtime status."""
    value = json_request(f"{_required_string(state, 'server_url')}/status")
    if not isinstance(value, dict):
        pytest.fail("Runtime /status did not return a JSON object")
    return value


def response_requests(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the fake sidecar's private response-chain audit through Docker exec."""
    result = subprocess.run(  # noqa: S603 - fixed probe against a harness-owned sidecar.
        [  # noqa: S607 - Docker is a required local runtime executable.
            "docker",
            "exec",
            _required_string(state, "fake_container"),
            "python",
            "-c",
            _RESPONSE_REQUESTS_PROBE,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.fail(f"Could not read deterministic sidecar response audit: {result.stderr}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Deterministic sidecar response audit was not JSON: {exc}")
    if not isinstance(value, dict):
        pytest.fail("Deterministic sidecar response audit was not a JSON object")
    requests = value.get("requests")
    if not isinstance(requests, list) or not all(isinstance(request, dict) for request in requests):
        pytest.fail("Deterministic sidecar response audit did not contain request objects")
    return requests


def wait_until[T](
    predicate: Callable[[], T | None],
    *,
    description: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> T:
    """Poll a public runtime observation until it has the expected value."""
    deadline = time.monotonic() + timeout_seconds
    last_value: object = None
    while time.monotonic() < deadline:
        value = predicate()
        if value is not None:
            return value
        last_value = value
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(f"Timed out waiting for {description}; last value: {last_value!r}")


def wait_for_response_count(
    state: dict[str, Any],
    jid: str,
    response_text: str,
    count: int,
) -> list[dict[str, Any]]:
    """Wait until persisted agent results reach ``count`` for a chat."""
    return wait_until(
        lambda: _messages_if_response_count(state, jid, response_text, count),
        description=f"{count} persisted deterministic responses for {jid}",
    )


def wait_for_ready(state: dict[str, Any]) -> dict[str, Any]:
    """Wait until a restarted runtime reports all core subsystems healthy."""
    return wait_until(
        lambda: _ready_status(state),
        description="runtime semantic readiness after restart",
        timeout_seconds=120.0,
    )


def wait_for_response_request(state: dict[str, Any], marker: str) -> dict[str, Any]:
    """Wait for the sidecar request whose input contains the unique test marker."""
    return wait_until(
        lambda: _response_request_with_marker(state, marker),
        description=f"deterministic sidecar request containing {marker}",
    )


def _state_path_from_environment() -> Path:
    value = os.environ.get("PYNCHY_RUNTIME_STATE")
    if not value:
        pytest.fail("Run runtime tests through scripts/runtime_harness.py run or exec")
    return Path(value)


def _required_string(state: dict[str, Any], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str):
        pytest.fail(f"Runtime state is missing string field {key!r}")
    return value


def _database_path(state: dict[str, Any]) -> str:
    return _required_string(state, "database_path")


def _messages_if_response_count(
    state: dict[str, Any], jid: str, response_text: str, count: int
) -> list[dict[str, Any]] | None:
    value = messages(state, jid)
    matching = [
        item
        for item in value
        if item.get("sender_name") == "pynchy" and item.get("content") == response_text
    ]
    return value if len(matching) >= count else None


def _response_request_with_marker(state: dict[str, Any], marker: str) -> dict[str, Any] | None:
    for request in response_requests(state):
        if _contains_marker(request.get("input"), marker):
            return request
    return None


def _contains_marker(value: object, marker: str) -> bool:
    if isinstance(value, str):
        return marker in value
    if isinstance(value, list):
        return any(_contains_marker(item, marker) for item in value)
    if isinstance(value, dict):
        return any(_contains_marker(item, marker) for item in value.values())
    return False


def _ready_status(state: dict[str, Any]) -> dict[str, Any] | None:
    value = status(state)
    service = value.get("service")
    gateway = value.get("gateway")
    temporal = value.get("temporal")
    if not all(isinstance(item, dict) for item in (service, gateway, temporal)):
        return None
    if (
        service.get("status") == "ok"
        and gateway.get("ready") is True
        and gateway.get("database") == "connected"
        and temporal.get("cluster_healthy") is True
        and temporal.get("worker_running") is True
    ):
        return value
    return None
