"""Payload codec and validation helpers for Obsidian learning reviews."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from pynchy.host.learning.packet_models import LearningPacket


def validate_job_id(job_id: str) -> None:
    if not job_id or job_id == "." or ".." in job_id or "/" in job_id or "\\" in job_id:
        raise ValueError("job_id must be a non-empty safe filename component")


def packet_to_payload(packet: LearningPacket) -> dict[str, Any]:
    return asdict(packet)


def packet_from_payload(payload: Mapping[str, Any]) -> LearningPacket:
    return LearningPacket(
        job_id=_required_job_id(payload, "job_id"),
        chat_jid=_required_str(payload, "chat_jid"),
        group_folder=_required_str(payload, "group_folder"),
        profile=_required_str(payload, "profile"),
        created_at=_required_str(payload, "created_at"),
        messages=_required_message_list(payload, "messages"),
        final_answer=_optional_str(payload, "final_answer"),
        tool_counts=_required_int_dict(payload, "tool_counts"),
        error_snippets=_required_str_list(payload, "error_snippets"),
        loaded_skills=_required_str_list(payload, "loaded_skills"),
        provenance=_required_str_dict(payload, "provenance"),
    )


def _required_job_id(payload: Mapping[str, Any], key: str) -> str:
    value = _required_str(payload, key)
    validate_job_id(value)
    return value


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload[key]
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"{key} must be a string or null")


def _required_str_list(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload[key]
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{key} items must be strings")
        result.append(item)
    return result


def _required_message_list(payload: Mapping[str, Any], key: str) -> list[dict[str, str]]:
    value = payload[key]
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError(f"{key} items must be objects")
        result.append(_str_dict_from_mapping(item, f"{key} item"))
    return result


def _required_str_dict(payload: Mapping[str, Any], key: str) -> dict[str, str]:
    value = payload[key]
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be an object")
    return _str_dict_from_mapping(value, key)


def _required_int_dict(payload: Mapping[str, Any], key: str) -> dict[str, int]:
    value = payload[key]
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be an object")
    result: dict[str, int] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise TypeError(f"{key} keys must be strings")
        if not isinstance(item_value, int) or isinstance(item_value, bool):
            raise TypeError(f"{key} values must be integers")
        result[item_key] = item_value
    return result


def _str_dict_from_mapping(value: Mapping[Any, Any], field_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise TypeError(f"{field_name} keys must be strings")
        if not isinstance(item_value, str):
            raise TypeError(f"{field_name} values must be strings")
        result[item_key] = item_value
    return result
