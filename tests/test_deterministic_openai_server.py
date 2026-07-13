"""End-to-end HTTP contract tests for the deterministic OpenAI sidecar."""

from __future__ import annotations

import contextlib
import json
import threading
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from scripts.deterministic_openai_server import DeterministicOpenAIServer

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextlib.contextmanager
def _server(response_text: str = "fixed answer") -> Iterator[str]:
    server = DeterministicOpenAIServer(("127.0.0.1", 0), response_text)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _json_request(
    base_url: str, path: str, body: dict[str, object]
) -> tuple[HTTPStatus, dict[str, object]]:
    request = Request(  # noqa: S310 - local test server request.
        f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=2) as response:  # noqa: S310 - local test server URL.
        return HTTPStatus(response.status), json.loads(response.read())


def _json_get(base_url: str, path: str) -> dict[str, object]:
    with urlopen(f"{base_url}{path}", timeout=2) as response:  # noqa: S310 - local test server URL.
        value = json.loads(response.read())
    assert isinstance(value, dict)
    return value


def test_server_advertises_model_and_returns_fixed_chat_completion() -> None:
    with _server() as base_url:
        with urlopen(f"{base_url}/v1/models", timeout=2) as response:  # noqa: S310 - local test server URL.
            models = json.loads(response.read())
        status, completion = _json_request(
            base_url,
            "/v1/chat/completions",
            {"model": "caller-selected-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert models["data"] == [
        {
            "id": "pynchy-deterministic",
            "object": "model",
            "created": 1_700_000_000,
            "owned_by": "pynchy-runtime",
        }
    ]
    assert status == HTTPStatus.OK
    assert completion["model"] == "caller-selected-model"
    assert completion["choices"][0]["message"] == {"role": "assistant", "content": "fixed answer"}
    assert completion["choices"][0]["finish_reason"] == "stop"


def test_server_streams_openai_response_events_with_fixed_text() -> None:
    with _server("streamed answer") as base_url:
        payload = {"model": "pynchy-deterministic", "input": "hi", "stream": True}
        request = Request(  # noqa: S310 - local test server request.
            f"{base_url}/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:  # noqa: S310 - local test server URL.
            raw_events = response.read().decode("utf-8").splitlines()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in raw_events
        if line.startswith("data: ") and line != "data: [DONE]"
    ]

    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[3]["delta"] == "streamed answer"
    assert events[0]["response"]["id"] == events[-1]["response"]["id"]
    assert events[-1]["response"]["status"] == "completed"
    assert "data: [DONE]" in raw_events


def test_server_records_a_deterministic_response_chain() -> None:
    """The harness can distinguish a warm chained turn from a static fake response."""
    with _server() as base_url:
        cold_payload = {"model": "pynchy-deterministic", "input": "cold marker"}
        cold_status, cold_response = _json_request(base_url, "/v1/responses", cold_payload)
        duplicate_status, duplicate_response = _json_request(
            base_url, "/v1/responses", cold_payload
        )
        warm_status, warm_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": "warm marker",
                "previous_response_id": cold_response["id"],
            },
        )
        audit = _json_get(base_url, "/__pynchy_runtime__/response-requests")

    assert cold_status == duplicate_status == warm_status == HTTPStatus.OK
    assert cold_response["id"] == duplicate_response["id"]
    assert warm_response["id"] != cold_response["id"]
    assert audit == {
        "requests": [
            {
                "response_id": cold_response["id"],
                "previous_response_id": None,
                "input": "cold marker",
            },
            {
                "response_id": duplicate_response["id"],
                "previous_response_id": None,
                "input": "cold marker",
            },
            {
                "response_id": warm_response["id"],
                "previous_response_id": cold_response["id"],
                "input": "warm marker",
            },
        ]
    }


def test_server_rejects_invalid_json_and_unknown_endpoints() -> None:
    with _server() as base_url:
        invalid = Request(  # noqa: S310 - local test server request.
            f"{base_url}/v1/chat/completions",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as invalid_error:
            urlopen(invalid, timeout=2)  # noqa: S310 - local test server URL.
        with pytest.raises(HTTPError) as unknown_error:
            urlopen(f"{base_url}/v1/unknown", timeout=2)  # noqa: S310 - local test server URL.

    assert invalid_error.value.code == HTTPStatus.BAD_REQUEST
    assert json.loads(invalid_error.value.read())["error"]["message"] == "Request body must be JSON"
    assert unknown_error.value.code == HTTPStatus.NOT_FOUND
    assert json.loads(unknown_error.value.read())["error"]["message"] == (
        "Unknown deterministic OpenAI endpoint"
    )
