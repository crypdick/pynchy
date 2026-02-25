"""Serialization helpers for host↔container IPC.

Converts ContainerInput to dict for JSON transport into the container,
and parses JSON output from the container back to ContainerOutput.

Both functions are field-driven (via ``dataclasses.fields``) so new
fields added to the dataclasses are automatically handled without
updating the serialization code.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from pynchy.types import ContainerInput, ContainerOutput


def _input_to_dict(input_data: ContainerInput) -> dict[str, Any]:
    """Convert ContainerInput to dict for the Python agent-runner.

    Includes all fields except those set to None.  The container-side
    ``ContainerInput.from_dict()`` applies dataclass defaults for any
    missing keys, so omitting None values is safe and keeps the wire
    format compact.
    """
    return {
        f.name: getattr(input_data, f.name)
        for f in dataclasses.fields(input_data)
        if getattr(input_data, f.name) is not None
    }


def _parse_container_output(json_str: str) -> ContainerOutput:
    """Parse JSON from the Python agent-runner into ContainerOutput.

    Unknown keys in the JSON are silently ignored (forward-compat).
    Missing keys use the dataclass defaults.
    """
    data = json.loads(json_str)
    known = {f.name for f in dataclasses.fields(ContainerOutput)}
    return ContainerOutput(**{k: v for k, v in data.items() if k in known})
