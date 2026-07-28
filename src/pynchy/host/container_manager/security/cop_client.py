"""Gateway transport for Cop inspection requests."""

from __future__ import annotations

import json

import aiohttp

from pynchy.host.container_manager import gateway as gateway_manager

_DEFAULT_COP_MODEL = "claude-haiku-4-5-20251001"
_cop_model = _DEFAULT_COP_MODEL
_cop_wire_api = "messages"


class CopGatewayUnavailableError(RuntimeError):
    """The configured LLM gateway cannot serve a Cop request."""


def configure_cop_gateway(*, model: str | None, wire_api: str) -> None:
    """Apply the resolved Cop transport selection at application composition."""
    global _cop_model, _cop_wire_api  # noqa: PLW0603 - one host process owns one Cop transport selection.
    _cop_model = model or _DEFAULT_COP_MODEL
    _cop_wire_api = wire_api


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def _parse_json_object(text: str) -> dict[str, object]:
    result = json.loads(_strip_json_fence(text))
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        raise ValueError("Cop response must be a JSON object")
    return result


def _responses_sse_text(payload: str) -> str:
    deltas: list[str] = []
    completed_text: str | None = None
    for line in payload.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        event = json.loads(line.removeprefix("data: "))
        if not isinstance(event, dict):
            continue
        if event.get("type") == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event.get("type") == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str):
                completed_text = text
    text = "".join(deltas) or completed_text
    if not text:
        raise TypeError("Cop Responses stream omitted text content")
    return text


def _responses_json_text(data: object) -> str:
    if not isinstance(data, dict):
        raise TypeError("Cop Responses result must be an object")
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            content = item.get("content") if isinstance(item, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                text = part.get("text") if isinstance(part, dict) else None
                part_type = part.get("type") if isinstance(part, dict) else None
                if part_type == "output_text" and isinstance(text, str):
                    return text
    raise TypeError("Cop Responses result omitted text content")


async def _request_messages(
    *,
    url: str,
    headers: dict[str, str],
    model: str,
    system_prompt: str,
    user_content: str,
) -> str:
    messages_headers = {**headers, "anthropic-version": "2023-06-01"}
    body = {
        "model": model,
        "max_tokens": 200,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"{url}/v1/messages", headers=messages_headers, json=body) as response,
    ):
        response.raise_for_status()
        data = await response.json()
    content = data.get("content") if isinstance(data, dict) else None
    first_content = content[0] if isinstance(content, list) and content else None
    text = first_content.get("text") if isinstance(first_content, dict) else None
    if not isinstance(text, str):
        raise TypeError("Cop Messages response omitted text content")
    return text


async def _request_responses(
    *,
    url: str,
    headers: dict[str, str],
    model: str,
    system_prompt: str,
    user_content: str,
) -> str:
    body = {
        "model": model,
        "max_output_tokens": 1000,
        "stream": True,
        "input": [
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_content}],
            },
        ],
    }
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"{url}/v1/responses", headers=headers, json=body) as response,
    ):
        response.raise_for_status()
        payload = await response.text()
    if payload.lstrip().startswith("data: "):
        return _responses_sse_text(payload)
    return _responses_json_text(json.loads(payload))


async def request_inspection(*, system_prompt: str, user_content: str) -> dict[str, object]:
    """Call the configured gateway and parse one JSON inspection result."""
    gateway = gateway_manager.get_gateway()
    if gateway is None:
        raise CopGatewayUnavailableError("No gateway available")

    url = f"http://localhost:{gateway.port}"
    headers = {"x-api-key": gateway.key, "content-type": "application/json"}
    if _cop_wire_api == "responses":
        text = await _request_responses(
            url=url,
            headers=headers,
            model=_cop_model,
            system_prompt=system_prompt,
            user_content=user_content,
        )
    else:
        text = await _request_messages(
            url=url,
            headers=headers,
            model=_cop_model,
            system_prompt=system_prompt,
            user_content=user_content,
        )
    return _parse_json_object(text)
