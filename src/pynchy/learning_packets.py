"""Shared models for Obsidian learning review payloads."""

from __future__ import annotations

from collections.abc import (
    Mapping,
)
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LearningPacket:
    job_id: str
    chat_jid: str
    group_folder: str
    profile: str
    created_at: str
    messages: list[dict[str, str]]
    final_answer: str | None
    tool_counts: dict[str, int]
    error_snippets: list[str]
    loaded_skills: list[str]
    provenance: dict[str, str]


_UNSAFE_JOB_ID = "job_id must be a non-empty safe filename component"
_FIELD_STRING_REQUIRED = "{key} must be a string"
_FIELD_OPTIONAL_STRING_REQUIRED = "{key} must be a string or null"
_FIELD_LIST_REQUIRED = "{key} must be a list"
_FIELD_LIST_ITEMS_STRING = "{key} items must be strings"
_FIELD_LIST_ITEMS_OBJECT = "{key} items must be objects"
_FIELD_OBJECT_REQUIRED = "{key} must be an object"
_FIELD_KEYS_STRING = "{key} keys must be strings"
_FIELD_VALUES_INTEGER = "{key} values must be integers"
_FIELD_VALUES_STRING = "{key} values must be strings"


def validate_job_id(job_id: str) -> None:
    if not job_id or job_id == "." or ".." in job_id or "/" in job_id or "\\" in job_id:
        raise ValueError(_UNSAFE_JOB_ID)


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
        raise TypeError(_FIELD_STRING_REQUIRED.format(key=key))
    return value


def _optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload[key]
    if value is None or isinstance(value, str):
        return value
    raise TypeError(_FIELD_OPTIONAL_STRING_REQUIRED.format(key=key))


def _required_str_list(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload[key]
    if not isinstance(value, list):
        raise TypeError(_FIELD_LIST_REQUIRED.format(key=key))
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(_FIELD_LIST_ITEMS_STRING.format(key=key))
        result.append(item)
    return result


def _required_message_list(payload: Mapping[str, Any], key: str) -> list[dict[str, str]]:
    value = payload[key]
    if not isinstance(value, list):
        raise TypeError(_FIELD_LIST_REQUIRED.format(key=key))
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError(_FIELD_LIST_ITEMS_OBJECT.format(key=key))
        result.append(_str_dict_from_mapping(item, f"{key} item"))
    return result


def _required_str_dict(payload: Mapping[str, Any], key: str) -> dict[str, str]:
    value = payload[key]
    if not isinstance(value, dict):
        raise TypeError(_FIELD_OBJECT_REQUIRED.format(key=key))
    return _str_dict_from_mapping(value, key)


def _required_int_dict(payload: Mapping[str, Any], key: str) -> dict[str, int]:
    value = payload[key]
    if not isinstance(value, dict):
        raise TypeError(_FIELD_OBJECT_REQUIRED.format(key=key))
    result: dict[str, int] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise TypeError(_FIELD_KEYS_STRING.format(key=key))
        if not isinstance(item_value, int) or isinstance(item_value, bool):
            raise TypeError(_FIELD_VALUES_INTEGER.format(key=key))
        result[item_key] = item_value
    return result


def _str_dict_from_mapping(value: Mapping[Any, Any], field_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise TypeError(_FIELD_KEYS_STRING.format(key=field_name))
        if not isinstance(item_value, str):
            raise TypeError(_FIELD_VALUES_STRING.format(key=field_name))
        result[item_key] = item_value
    return result
