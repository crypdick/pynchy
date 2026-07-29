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
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
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


def test_server_streams_a_scripted_patch_probe_before_its_completion() -> None:
    with _server() as base_url:
        payload = {
            "model": "pynchy-deterministic",
            "input": "PYNCHY_RUNTIME_PATCH_PROBE",
            "stream": True,
        }
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
        "response.output_item.done",
        "response.completed",
    ]
    assert events[1]["item"]["type"] == "apply_patch_call"
    assert events[1]["item"]["operation"] == {
        "type": "create_file",
        "path": "/workspace/group/runtime-patch-proof.txt",
        "diff": "+PYNCHY_RUNTIME_PATCH_OK",
    }
    assert events[-1]["response"]["output"] == [events[1]["item"]]


def test_server_streams_a_scripted_shell_probe_before_its_completion() -> None:
    with _server() as base_url:
        payload = {
            "model": "pynchy-deterministic",
            "input": "PYNCHY_RUNTIME_SHELL_PROBE",
            "stream": True,
        }
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
        "response.output_item.done",
        "response.completed",
    ]
    assert events[1]["item"] == {
        "id": "item_runtime_shell_probe",
        "type": "shell_call",
        "status": "completed",
        "call_id": "call_runtime_shell_probe",
        "action": {
            "commands": [
                "printf PYNCHY_RUNTIME_SHELL_OK > /workspace/group/runtime-shell-proof.txt"
            ],
            "timeout_ms": 5_000,
        },
    }
    assert events[-1]["response"]["output"] == [events[1]["item"]]


def test_server_completes_a_shell_output_probe_only_after_receiving_stdout() -> None:
    with _server() as base_url:
        initial_status, initial_response = _json_request(
            base_url,
            "/v1/responses",
            {"model": "pynchy-deterministic", "input": "PYNCHY_RUNTIME_SHELL_OUTPUT_PROBE"},
        )
        missing_output_status, missing_output_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": [{"call_id": "call_runtime_shell_output_probe"}],
                "previous_response_id": initial_response["id"],
            },
        )
        completed_status, completed_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": [
                    {
                        "call_id": "call_runtime_shell_output_probe",
                        "output": [{"stdout": "PYNCHY_RUNTIME_SHELL_OUTPUT_OK"}],
                    }
                ],
                "previous_response_id": initial_response["id"],
            },
        )

    assert initial_status == missing_output_status == completed_status == HTTPStatus.OK
    assert initial_response["output"][0]["action"]["commands"] == [
        "printf PYNCHY_RUNTIME_SHELL_OUTPUT_OK"
    ]
    assert missing_output_response["output"][0]["content"][0]["text"] == "fixed answer"
    assert completed_response["output"][0]["content"][0]["text"] == "PYNCHY_RUNTIME_SHELL_OUTPUT_OK"


def test_server_completes_a_bounded_shell_probe_only_after_receiving_truncated_stdout() -> None:
    with _server() as base_url:
        initial_status, initial_response = _json_request(
            base_url,
            "/v1/responses",
            {"model": "pynchy-deterministic", "input": "PYNCHY_RUNTIME_SHELL_LIMIT_PROBE"},
        )
        completed_status, completed_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": [
                    {
                        "call_id": "call_runtime_shell_limit_probe",
                        "output": [{"stdout": "PYNCHY_R"}],
                    }
                ],
                "previous_response_id": initial_response["id"],
            },
        )

    assert initial_status == completed_status == HTTPStatus.OK
    assert initial_response["output"][0]["action"] == {
        "commands": ["printf PYNCHY_RUNTIME_SHELL_LIMIT_OUTPUT"],
        "max_output_length": 8,
        "timeout_ms": 5_000,
    }
    assert completed_response["output"][0]["content"][0]["text"] == "PYNCHY_RUNTIME_SHELL_LIMIT_OK"


def test_server_completes_a_shell_timeout_probe_only_after_receiving_timeout_outcome() -> None:
    with _server() as base_url:
        initial_status, initial_response = _json_request(
            base_url,
            "/v1/responses",
            {"model": "pynchy-deterministic", "input": "PYNCHY_RUNTIME_SHELL_TIMEOUT_PROBE"},
        )
        completed_status, completed_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": [
                    {
                        "call_id": "call_runtime_shell_timeout_probe",
                        "output": [{"outcome": {"type": "timeout"}}],
                    }
                ],
                "previous_response_id": initial_response["id"],
            },
        )

    assert initial_status == completed_status == HTTPStatus.OK
    assert initial_response["output"][0]["action"] == {
        "commands": ["sleep 1"],
        "timeout_ms": 10,
    }
    assert completed_response["output"][0]["content"][0]["text"] == (
        "PYNCHY_RUNTIME_SHELL_TIMEOUT_REPORTED"
    )


def test_server_runs_a_chained_shell_probe_before_its_final_response() -> None:
    with _server() as base_url:
        first_status, first_response = _json_request(
            base_url,
            "/v1/responses",
            {"model": "pynchy-deterministic", "input": "PYNCHY_RUNTIME_SHELL_CHAIN_PROBE"},
        )
        second_status, second_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": [
                    {
                        "call_id": "call_runtime_shell_chain_first",
                        "output": [{"stdout": "PYNCHY_RUNTIME_SHELL_CHAIN_FIRST"}],
                    }
                ],
                "previous_response_id": first_response["id"],
            },
        )
        completed_status, completed_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": [
                    {
                        "call_id": "call_runtime_shell_chain_second",
                        "output": [{"stdout": "PYNCHY_RUNTIME_SHELL_CHAIN_SECOND"}],
                    }
                ],
                "previous_response_id": second_response["id"],
            },
        )

    assert first_status == second_status == completed_status == HTTPStatus.OK
    assert first_response["output"][0]["call_id"] == "call_runtime_shell_chain_first"
    assert second_response["output"][0]["call_id"] == "call_runtime_shell_chain_second"
    assert completed_response["output"][0]["content"][0]["text"] == "PYNCHY_RUNTIME_SHELL_CHAIN_OK"


def test_server_completes_a_shell_failure_probe_only_after_receiving_stderr_and_exit_code() -> None:
    with _server() as base_url:
        initial_status, initial_response = _json_request(
            base_url,
            "/v1/responses",
            {"model": "pynchy-deterministic", "input": "PYNCHY_RUNTIME_SHELL_FAILURE_PROBE"},
        )
        completed_status, completed_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": [
                    {
                        "call_id": "call_runtime_shell_failure_probe",
                        "output": [
                            {
                                "stdout": "",
                                "stderr": "PYNCHY_RUNTIME_SHELL_FAILURE_STDERR",
                                "outcome": {"type": "exit", "exit_code": 7},
                            }
                        ],
                    }
                ],
                "previous_response_id": initial_response["id"],
            },
        )

    assert initial_status == completed_status == HTTPStatus.OK
    assert initial_response["output"][0]["action"]["commands"] == [
        "printf PYNCHY_RUNTIME_SHELL_FAILURE_STDERR >&2; exit 7"
    ]
    assert completed_response["output"][0]["content"][0]["text"] == (
        "PYNCHY_RUNTIME_SHELL_FAILURE_REPORTED"
    )


def test_server_completes_a_multi_command_probe_only_after_ordered_outputs() -> None:
    with _server() as base_url:
        initial_status, initial_response = _json_request(
            base_url,
            "/v1/responses",
            {"model": "pynchy-deterministic", "input": "PYNCHY_RUNTIME_SHELL_MULTI_PROBE"},
        )
        completed_status, completed_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "pynchy-deterministic",
                "input": [
                    {
                        "call_id": "call_runtime_shell_multi_probe",
                        "output": [
                            {"stdout": "PYNCHY_RUNTIME_SHELL_MULTI_FIRST"},
                            {"stdout": "PYNCHY_RUNTIME_SHELL_MULTI_SECOND"},
                        ],
                    }
                ],
                "previous_response_id": initial_response["id"],
            },
        )

    assert initial_status == completed_status == HTTPStatus.OK
    assert initial_response["output"][0]["action"]["commands"] == [
        "printf PYNCHY_RUNTIME_SHELL_MULTI_FIRST",
        "printf PYNCHY_RUNTIME_SHELL_MULTI_SECOND",
    ]
    assert completed_response["output"][0]["content"][0]["text"] == (
        "PYNCHY_RUNTIME_SHELL_MULTI_OK"
    )


def test_server_returns_a_patch_update_probe() -> None:
    with _server() as base_url:
        status, response = _json_request(
            base_url,
            "/v1/responses",
            {"model": "pynchy-deterministic", "input": "PYNCHY_RUNTIME_PATCH_UPDATE_PROBE"},
        )

    assert status == HTTPStatus.OK
    assert response["output"] == [
        {
            "id": "item_runtime_patch_update_probe",
            "type": "apply_patch_call",
            "status": "completed",
            "call_id": "call_runtime_patch_update_probe",
            "operation": {
                "type": "update_file",
                "path": "/workspace/group/runtime-patch-update.txt",
                "diff": "@@\n-seed\n+PYNCHY_RUNTIME_PATCH_UPDATE_OK",
            },
        }
    ]


def test_server_returns_a_patch_delete_probe() -> None:
    with _server() as base_url:
        status, response = _json_request(
            base_url,
            "/v1/responses",
            {"model": "pynchy-deterministic", "input": "PYNCHY_RUNTIME_PATCH_DELETE_PROBE"},
        )

    assert status == HTTPStatus.OK
    assert response["output"] == [
        {
            "id": "item_runtime_patch_delete_probe",
            "type": "apply_patch_call",
            "status": "completed",
            "call_id": "call_runtime_patch_delete_probe",
            "operation": {
                "type": "delete_file",
                "path": "/workspace/group/runtime-patch-delete.txt",
            },
        }
    ]


def test_server_records_a_deterministic_response_chain() -> None:
    """The harness can distinguish a warm chained turn from a static fake response."""
    with _server() as base_url:
        cold_payload = {
            "model": "caller-selected-model",
            "input": "cold marker",
            "stream": False,
            "max_output_tokens": 1,
        }
        cold_status, cold_response = _json_request(base_url, "/v1/responses", cold_payload)
        duplicate_status, duplicate_response = _json_request(
            base_url, "/v1/responses", cold_payload
        )
        warm_status, warm_response = _json_request(
            base_url,
            "/v1/responses",
            {
                "model": "caller-selected-model",
                "input": "warm marker",
                "previous_response_id": cold_response["id"],
                "stream": False,
                "max_output_tokens": 2,
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
                "model": "caller-selected-model",
                "input": "cold marker",
                "stream": False,
                "max_output_tokens": 1,
            },
            {
                "response_id": duplicate_response["id"],
                "previous_response_id": None,
                "model": "caller-selected-model",
                "input": "cold marker",
                "stream": False,
                "max_output_tokens": 1,
            },
            {
                "response_id": warm_response["id"],
                "previous_response_id": cold_response["id"],
                "model": "caller-selected-model",
                "input": "warm marker",
                "stream": False,
                "max_output_tokens": 2,
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
