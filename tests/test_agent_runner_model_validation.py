"""Public validation of the agent runner container wire models."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"),
)

from agent_runner.models import ContainerInput, ContainerOutput


def _input_data() -> dict[str, object]:
    return {
        "messages": [],
        "group_folder": "group",
        "chat_jid": "slack:group",
        "is_admin": False,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("messages", "not a list"), ("agent_core_config", [])],
)
def test_container_input_rejects_values_with_the_wrong_shape(
    field: str,
    value: object,
) -> None:
    data = _input_data() | {field: value}

    with pytest.raises(TypeError, match=f"ContainerInput.{field}"):
        ContainerInput.from_dict(data)


def test_container_input_accepts_an_empty_typed_mapping() -> None:
    input_data = ContainerInput.from_dict(_input_data() | {"agent_core_config": {}})

    assert input_data.agent_core_config == {}


def test_container_input_preserves_explicit_repository_scopes() -> None:
    input_data = ContainerInput.from_dict(_input_data() | {"repo_accesses": []})

    assert input_data.repo_accesses == []


def test_container_output_includes_a_query_id_in_the_wire_shape() -> None:
    output = ContainerOutput(status="success", result="done", query_id="query-1")

    assert output.to_dict() == {
        "type": "result",
        "status": "success",
        "query_id": "query-1",
        "result": "done",
    }
