#!/usr/bin/env python3
"""Serve a small deterministic OpenAI-compatible API for runtime harnesses."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_DEFAULT_RESPONSE_TEXT = "Pynchy deterministic response."
_RESPONSE_TEXT_ENV = "PYNCHY_DETERMINISTIC_RESPONSE"
_MODEL_NAME = "pynchy-deterministic"
_CREATED_AT = 1_700_000_000
_MESSAGE_ID = "msg_deterministic"
_RESPONSE_REQUESTS_PATH = "/__pynchy_runtime__/response-requests"
_RESPONSE_ID_DIGEST_LENGTH = 32
_PATCH_PROBE_MARKER = "PYNCHY_RUNTIME_PATCH_PROBE"
_PATCH_PROBE_RESPONSE = "PYNCHY_RUNTIME_PATCH_OK"
_PATCH_PROBE_CALL_ID = "call_runtime_patch_probe"
_SHELL_PROBE_MARKER = "PYNCHY_RUNTIME_SHELL_PROBE"
_SHELL_PROBE_RESPONSE = "PYNCHY_RUNTIME_SHELL_OK"
_SHELL_PROBE_CALL_ID = "call_runtime_shell_probe"
_SHELL_OUTPUT_PROBE_MARKER = "PYNCHY_RUNTIME_SHELL_OUTPUT_PROBE"
_SHELL_OUTPUT_PROBE_RESPONSE = "PYNCHY_RUNTIME_SHELL_OUTPUT_OK"
_SHELL_OUTPUT_PROBE_CALL_ID = "call_runtime_shell_output_probe"
_SHELL_FAILURE_PROBE_MARKER = "PYNCHY_RUNTIME_SHELL_FAILURE_PROBE"
_SHELL_FAILURE_PROBE_RESPONSE = "PYNCHY_RUNTIME_SHELL_FAILURE_REPORTED"
_SHELL_FAILURE_PROBE_CALL_ID = "call_runtime_shell_failure_probe"
_SHELL_FAILURE_PROBE_STDERR = "PYNCHY_RUNTIME_SHELL_FAILURE_STDERR"
_SHELL_MULTI_PROBE_MARKER = "PYNCHY_RUNTIME_SHELL_MULTI_PROBE"
_SHELL_MULTI_PROBE_RESPONSE = "PYNCHY_RUNTIME_SHELL_MULTI_OK"
_SHELL_MULTI_PROBE_CALL_ID = "call_runtime_shell_multi_probe"
_SHELL_MULTI_PROBE_OUTPUT = (
    "PYNCHY_RUNTIME_SHELL_MULTI_FIRST",
    "PYNCHY_RUNTIME_SHELL_MULTI_SECOND",
)
_PATCH_UPDATE_PROBE_MARKER = "PYNCHY_RUNTIME_PATCH_UPDATE_PROBE"
_PATCH_UPDATE_PROBE_RESPONSE = "PYNCHY_RUNTIME_PATCH_UPDATE_OK"
_PATCH_UPDATE_PROBE_CALL_ID = "call_runtime_patch_update_probe"


class DeterministicOpenAIServer(ThreadingHTTPServer):
    """HTTP server carrying fixed answers and a private request audit for runtime tests."""

    def __init__(self, server_address: tuple[str, int], response_text: str) -> None:
        super().__init__(server_address, DeterministicOpenAIHandler)
        self.response_text = response_text
        self._response_requests: list[dict[str, Any]] = []
        self._response_requests_lock = threading.Lock()

    def record_response_request(self, payload: dict[str, Any], response_id: str) -> None:
        """Keep the response-chain observation private to the harness network."""
        request = {
            "response_id": response_id,
            "previous_response_id": payload.get("previous_response_id"),
            "input": payload.get("input"),
        }
        with self._response_requests_lock:
            self._response_requests.append(request)

    def response_requests(self) -> list[dict[str, Any]]:
        """Return a shallow snapshot suitable for the sidecar's debug endpoint."""
        with self._response_requests_lock:
            return list(self._response_requests)


class DeterministicOpenAIHandler(BaseHTTPRequestHandler):
    """Handle the OpenAI endpoints Pynchy reaches through LiteLLM."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path in {"/v1/models", "/models"}:
            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": _MODEL_NAME,
                            "object": "model",
                            "created": _CREATED_AT,
                            "owned_by": "pynchy-runtime",
                        }
                    ],
                },
            )
            return
        if self.path == _RESPONSE_REQUESTS_PATH:
            self._send_json(HTTPStatus.OK, {"requests": self._server.response_requests()})
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Unknown deterministic OpenAI endpoint")

    def do_POST(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return

        if self.path in {"/v1/chat/completions", "/chat/completions"}:
            self._handle_chat_completion(payload)
            return
        if self.path in {"/v1/responses", "/responses"}:
            self._handle_response(payload)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Unknown deterministic OpenAI endpoint")

    def log_message(self, _message_format: str, *_args: object) -> None:
        """Keep deterministic sidecar logs focused on startup failures."""

    def _read_json_body(self) -> dict[str, Any] | None:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_length)
        except ValueError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None

        try:
            payload = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be JSON")
            return None
        if not isinstance(payload, dict):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")
            return None
        return payload

    def _handle_chat_completion(self, payload: dict[str, Any]) -> None:
        if payload.get("stream") is True:
            self._stream_chat_completion(payload)
            return
        self._send_json(HTTPStatus.OK, self._chat_completion(payload))

    def _handle_response(self, payload: dict[str, Any]) -> None:
        response_id = _response_id(payload)
        self._server.record_response_request(payload, response_id)
        if payload.get("stream") is True:
            self._stream_response(payload, response_id)
            return
        self._send_json(
            HTTPStatus.OK,
            self._response(payload, response_id=response_id, status="completed"),
        )

    def _chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = _requested_model(payload)
        return {
            "id": "chatcmpl_deterministic",
            "object": "chat.completion",
            "created": _CREATED_AT,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self._response_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": _usage(),
        }

    def _stream_chat_completion(self, payload: dict[str, Any]) -> None:
        model = _requested_model(payload)
        self._start_event_stream()
        self._send_event(
            {
                "id": "chatcmpl_deterministic",
                "object": "chat.completion.chunk",
                "created": _CREATED_AT,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": self._response_text},
                        "finish_reason": None,
                    }
                ],
            }
        )
        self._send_event(
            {
                "id": "chatcmpl_deterministic",
                "object": "chat.completion.chunk",
                "created": _CREATED_AT,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        self._finish_event_stream()

    def _response(
        self, payload: dict[str, Any], *, response_id: str, status: str
    ) -> dict[str, Any]:
        return _response_payload(
            payload, response_id, _response_text(payload, self._response_text), status
        )

    def _stream_response(self, payload: dict[str, Any], response_id: str) -> None:
        self._start_event_stream()
        for sequence, (event_type, fields) in enumerate(
            _response_stream_events(payload, response_id, self._response_text), start=1
        ):
            self._send_response_event(event_type, fields, sequence)
        self._finish_event_stream()

    @property
    def _server(self) -> DeterministicOpenAIServer:
        server = self.server
        if not isinstance(server, DeterministicOpenAIServer):
            raise TypeError("Deterministic OpenAI handler requires its dedicated server")
        return server

    @property
    def _response_text(self) -> str:
        return self._server.response_text

    def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": {"message": message, "type": "invalid_request_error"}})

    def _start_event_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_event(self, body: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(body, separators=(',', ':'))}\n\n".encode())
        self.wfile.flush()

    def _send_response_event(
        self,
        event_type: str,
        fields: dict[str, Any],
        sequence_number: int,
    ) -> None:
        self._send_event({"type": event_type, "sequence_number": sequence_number, **fields})

    def _finish_event_stream(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def _requested_model(payload: dict[str, Any]) -> str:
    model = payload.get("model")
    return model if isinstance(model, str) and model else _MODEL_NAME


def _response_id(payload: dict[str, Any]) -> str:
    """Make response IDs stable across sidecar restarts without conflating turns."""
    canonical_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    return f"resp_{digest[:_RESPONSE_ID_DIGEST_LENGTH]}"


def _output_message(text: str) -> dict[str, Any]:
    return {
        "id": _MESSAGE_ID,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _response_stream_events(
    payload: dict[str, Any], response_id: str, default_text: str
) -> tuple[tuple[str, dict[str, Any]], ...]:
    response_text = _response_text(payload, default_text)
    completed = _response_payload(payload, response_id, response_text, "completed")
    started = _response_payload(payload, response_id, response_text, "in_progress")
    if item := _probe_tool_call(payload):
        return (
            ("response.created", {"response": started}),
            ("response.output_item.added", {"output_index": 0, "item": item}),
            ("response.output_item.done", {"output_index": 0, "item": item}),
            ("response.completed", {"response": completed}),
        )

    item = _output_message("")
    item["status"] = "in_progress"
    completed_item = _output_message(response_text)
    part = {"type": "output_text", "text": "", "annotations": []}
    completed_part = {"type": "output_text", "text": response_text, "annotations": []}
    return (
        ("response.created", {"response": started}),
        ("response.output_item.added", {"output_index": 0, "item": item}),
        (
            "response.content_part.added",
            {"item_id": _MESSAGE_ID, "output_index": 0, "content_index": 0, "part": part},
        ),
        (
            "response.output_text.delta",
            {"item_id": _MESSAGE_ID, "output_index": 0, "content_index": 0, "delta": response_text},
        ),
        (
            "response.output_text.done",
            {"item_id": _MESSAGE_ID, "output_index": 0, "content_index": 0, "text": response_text},
        ),
        (
            "response.content_part.done",
            {"item_id": _MESSAGE_ID, "output_index": 0, "content_index": 0, "part": completed_part},
        ),
        ("response.output_item.done", {"output_index": 0, "item": completed_item}),
        ("response.completed", {"response": completed}),
    )


def _response_payload(
    payload: dict[str, Any], response_id: str, response_text: str, status: str
) -> dict[str, Any]:
    completed = status == "completed"
    return {
        "id": response_id,
        "object": "response",
        "created_at": _CREATED_AT,
        "completed_at": _CREATED_AT if completed else None,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": _requested_model(payload),
        "output": _response_output(payload, response_text) if completed else [],
        "parallel_tool_calls": True,
        "previous_response_id": payload.get("previous_response_id"),
        "reasoning": {"effort": None, "summary": None},
        "store": True,
        "temperature": 1,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1,
        "truncation": "disabled",
        "usage": _usage() if completed else None,
        "user": None,
        "metadata": {},
    }


def _response_output(payload: dict[str, Any], response_text: str) -> list[dict[str, Any]]:
    return [_probe_tool_call(payload) or _output_message(response_text)]


def _response_text(payload: dict[str, Any], default_text: str) -> str:
    if _contains(payload.get("input"), _PATCH_UPDATE_PROBE_CALL_ID):
        return _PATCH_UPDATE_PROBE_RESPONSE
    if _contains(payload.get("input"), _SHELL_MULTI_PROBE_CALL_ID) and _has_shell_stdout_sequence(
        payload.get("input"), _SHELL_MULTI_PROBE_OUTPUT
    ):
        return _SHELL_MULTI_PROBE_RESPONSE
    if (
        _contains(payload.get("input"), _SHELL_FAILURE_PROBE_CALL_ID)
        and _contains(payload.get("input"), _SHELL_FAILURE_PROBE_STDERR)
        and _contains_shell_exit_code(payload.get("input"), 7)
    ):
        return _SHELL_FAILURE_PROBE_RESPONSE
    if _contains(payload.get("input"), _SHELL_OUTPUT_PROBE_CALL_ID) and _contains(
        payload.get("input"), _SHELL_OUTPUT_PROBE_RESPONSE
    ):
        return _SHELL_OUTPUT_PROBE_RESPONSE
    if _contains(payload.get("input"), _SHELL_PROBE_CALL_ID):
        return _SHELL_PROBE_RESPONSE
    return (
        _PATCH_PROBE_RESPONSE
        if _contains(payload.get("input"), _PATCH_PROBE_CALL_ID)
        else default_text
    )


def _is_patch_probe_request(payload: dict[str, Any]) -> bool:
    return _contains(payload.get("input"), _PATCH_PROBE_MARKER) and not _contains(
        payload.get("input"), _PATCH_PROBE_CALL_ID
    )


def _is_shell_probe_request(payload: dict[str, Any]) -> bool:
    return _contains(payload.get("input"), _SHELL_PROBE_MARKER) and not _contains(
        payload.get("input"), _SHELL_PROBE_CALL_ID
    )


def _is_shell_output_probe_request(payload: dict[str, Any]) -> bool:
    return _contains(payload.get("input"), _SHELL_OUTPUT_PROBE_MARKER) and not _contains(
        payload.get("input"), _SHELL_OUTPUT_PROBE_CALL_ID
    )


def _is_shell_failure_probe_request(payload: dict[str, Any]) -> bool:
    return _contains(payload.get("input"), _SHELL_FAILURE_PROBE_MARKER) and not _contains(
        payload.get("input"), _SHELL_FAILURE_PROBE_CALL_ID
    )


def _is_shell_multi_probe_request(payload: dict[str, Any]) -> bool:
    return _contains(payload.get("input"), _SHELL_MULTI_PROBE_MARKER) and not _contains(
        payload.get("input"), _SHELL_MULTI_PROBE_CALL_ID
    )


def _is_patch_update_probe_request(payload: dict[str, Any]) -> bool:
    return _contains(payload.get("input"), _PATCH_UPDATE_PROBE_MARKER) and not _contains(
        payload.get("input"), _PATCH_UPDATE_PROBE_CALL_ID
    )


def _probe_tool_call(payload: dict[str, Any]) -> dict[str, Any] | None:
    for matches, tool_call in (
        (_is_patch_probe_request, _patch_probe_call),
        (_is_shell_probe_request, _shell_probe_call),
        (_is_shell_output_probe_request, _shell_output_probe_call),
        (_is_shell_failure_probe_request, _shell_failure_probe_call),
        (_is_shell_multi_probe_request, _shell_multi_probe_call),
        (_is_patch_update_probe_request, _patch_update_probe_call),
    ):
        if matches(payload):
            return tool_call()
    return None


def _patch_probe_call() -> dict[str, Any]:
    return {
        "id": "item_runtime_patch_probe",
        "type": "apply_patch_call",
        "status": "completed",
        "call_id": _PATCH_PROBE_CALL_ID,
        "operation": {
            "type": "create_file",
            "path": "/workspace/group/runtime-patch-proof.txt",
            "diff": "+PYNCHY_RUNTIME_PATCH_OK",
        },
    }


def _patch_update_probe_call() -> dict[str, Any]:
    return {
        "id": "item_runtime_patch_update_probe",
        "type": "apply_patch_call",
        "status": "completed",
        "call_id": _PATCH_UPDATE_PROBE_CALL_ID,
        "operation": {
            "type": "update_file",
            "path": "/workspace/group/runtime-patch-update.txt",
            "diff": "@@\n-seed\n+PYNCHY_RUNTIME_PATCH_UPDATE_OK",
        },
    }


def _shell_probe_call() -> dict[str, Any]:
    return {
        "id": "item_runtime_shell_probe",
        "type": "shell_call",
        "status": "completed",
        "call_id": _SHELL_PROBE_CALL_ID,
        "action": {
            "commands": [
                "printf PYNCHY_RUNTIME_SHELL_OK > /workspace/group/runtime-shell-proof.txt"
            ],
            "timeout_ms": 5_000,
        },
    }


def _shell_output_probe_call() -> dict[str, Any]:
    return {
        "id": "item_runtime_shell_output_probe",
        "type": "shell_call",
        "status": "completed",
        "call_id": _SHELL_OUTPUT_PROBE_CALL_ID,
        "action": {"commands": ["printf PYNCHY_RUNTIME_SHELL_OUTPUT_OK"], "timeout_ms": 5_000},
    }


def _shell_failure_probe_call() -> dict[str, Any]:
    return {
        "id": "item_runtime_shell_failure_probe",
        "type": "shell_call",
        "status": "completed",
        "call_id": _SHELL_FAILURE_PROBE_CALL_ID,
        "action": {
            "commands": [f"printf {_SHELL_FAILURE_PROBE_STDERR} >&2; exit 7"],
            "timeout_ms": 5_000,
        },
    }


def _shell_multi_probe_call() -> dict[str, Any]:
    return {
        "id": "item_runtime_shell_multi_probe",
        "type": "shell_call",
        "status": "completed",
        "call_id": _SHELL_MULTI_PROBE_CALL_ID,
        "action": {"commands": [f"printf {output}" for output in _SHELL_MULTI_PROBE_OUTPUT]},
    }


def _contains(value: object, marker: str) -> bool:
    if isinstance(value, str):
        return marker in value
    if isinstance(value, list):
        return any(_contains(item, marker) for item in value)
    if isinstance(value, dict):
        return any(_contains(item, marker) for item in value.values())
    return False


def _contains_shell_exit_code(value: object, expected: int) -> bool:
    if isinstance(value, list):
        return any(_contains_shell_exit_code(item, expected) for item in value)
    if not isinstance(value, dict):
        return False
    return (value.get("type") == "exit" and value.get("exit_code") == expected) or any(
        _contains_shell_exit_code(item, expected) for item in value.values()
    )


def _has_shell_stdout_sequence(value: object, expected: tuple[str, ...]) -> bool:
    if isinstance(value, list):
        return any(_has_shell_stdout_sequence(item, expected) for item in value)
    if not isinstance(value, dict):
        return False
    output = value.get("output")
    if isinstance(output, list):
        stdout = tuple(item.get("stdout") for item in output if isinstance(item, dict))
        if stdout == expected:
            return True
    return any(_has_shell_stdout_sequence(item, expected) for item in value.values())


def _usage() -> dict[str, Any]:
    return {
        "input_tokens": 1,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 1,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 2,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - Docker sidecar must accept its private network.
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    response_text = os.environ.get(_RESPONSE_TEXT_ENV, _DEFAULT_RESPONSE_TEXT)
    server = DeterministicOpenAIServer((args.host, args.port), response_text)
    server.serve_forever()


if __name__ == "__main__":
    main()
