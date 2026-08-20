"""Real-process coverage for the deterministic Pynchy runtime profile."""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - test invokes the repository-local harness CLI.
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ._helpers import response_requests

pytestmark = pytest.mark.runtime


def _state() -> dict[str, Any]:
    state_path = os.environ.get("PYNCHY_RUNTIME_STATE")
    if not state_path:
        pytest.fail("Run this test through scripts/runtime_harness.py run -- pytest -m runtime")
    value = json.loads(Path(state_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        pytest.fail(f"Runtime state must be a JSON object: {state_path}")
    return value


def _json_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = Request(  # noqa: S310 - harness supplies loopback URLs only.
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers or {},
        method=method,
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - harness supplies loopback URLs only.
        value = json.loads(response.read())
    if not isinstance(value, dict):
        pytest.fail(f"Expected JSON object from {url}")
    return value


def _gateway_headers(state: dict[str, Any]) -> dict[str, str]:
    key = state.get("gateway_key")
    if not isinstance(key, str):
        pytest.fail("Runtime state is missing gateway_key")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def test_runtime_is_semantically_ready_and_returns_the_fixed_chat_response() -> None:
    state = _state()
    response_text = state["response_text"]
    status = _json_request(f"{state['server_url']}/status")

    assert status["service"]["status"] == "ok"
    gateway = status["gateway"]
    assert gateway["litellm_container"] == "running"
    assert gateway["postgres_container"] == "running"
    assert gateway["ready"] is True
    assert gateway["database"] == "connected"
    assert status["temporal"]["cluster_healthy"] is True
    assert status["temporal"]["worker_running"] is True

    assert response_requests(state) == []

    responses_request = Request(  # noqa: S310 - harness supplies loopback URLs only.
        f"{state['gateway_url']}/v1/responses",
        data=json.dumps(
            {
                "model": state["model"],
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "."}],
                    }
                ],
                "stream": True,
                "max_output_tokens": 1,
            }
        ).encode("utf-8"),
        headers=_gateway_headers(state),
        method="POST",
    )
    with urlopen(responses_request, timeout=10) as response:  # noqa: S310 - loopback only.
        stream = response.read()
    assert b"data: [DONE]" in stream

    requests = response_requests(state)
    assert len(requests) == 1
    explicit_canary = requests[0]
    assert set(explicit_canary) == {
        "response_id",
        "previous_response_id",
        "model",
        "input",
        "stream",
        "max_output_tokens",
    }
    assert isinstance(explicit_canary["response_id"], str)
    assert explicit_canary["previous_response_id"] is None
    assert explicit_canary["model"] == state["model"]
    assert explicit_canary["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "."}],
        }
    ]
    assert explicit_canary["stream"] is True
    assert explicit_canary["max_output_tokens"] == 1

    completion = _json_request(
        f"{state['gateway_url']}/v1/chat/completions",
        method="POST",
        headers=_gateway_headers(state),
        body={
            "model": state["model"],
            "messages": [{"role": "user", "content": "runtime contract"}],
        },
    )

    assert completion["choices"][0]["message"]["content"] == response_text


def test_runtime_gateway_rejects_requests_without_the_ephemeral_key() -> None:
    state = _state()
    request = Request(  # noqa: S310 - harness supplies loopback URLs only.
        f"{state['gateway_url']}/v1/chat/completions",
        data=json.dumps(
            {
                "model": state["model"],
                "messages": [{"role": "user", "content": "unauthenticated runtime contract"}],
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=10)  # noqa: S310 - harness supplies loopback URLs only.

    assert error.value.code == 401


def test_runtime_forwards_streamed_responses_events() -> None:
    state = _state()
    request = Request(  # noqa: S310 - harness supplies loopback URLs only.
        f"{state['gateway_url']}/v1/responses",
        data=json.dumps(
            {
                "model": state["model"],
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "streamed runtime contract"}],
                    }
                ],
                "stream": True,
            }
        ).encode("utf-8"),
        headers=_gateway_headers(state),
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - harness supplies loopback URLs only.
        payload = response.read().decode("utf-8")

    assert '"type":"response.output_text.delta"' in payload
    assert state["response_text"] in payload
    assert "data: [DONE]" in payload


@pytest.mark.timeout(60)
def test_runtime_exec_requires_live_marker_backed_processes() -> None:
    """The manual harness command works only while its owned runtime is actually live."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(  # fixed repository-local harness command.
        [  # noqa: S607 - uv is the repository's required Python runner.
            "uv",
            "run",
            "python",
            "scripts/runtime_harness.py",
            "exec",
            "--",
            "uv",
            "run",
            "python",
            "-c",
            "import os; assert os.environ['PYNCHY_RUNTIME_STATE']; print('live')",
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "live"
