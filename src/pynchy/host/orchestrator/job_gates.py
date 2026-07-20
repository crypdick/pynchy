"""Shared scheduled-job wake-gate parsing."""

from __future__ import annotations

import json


def parse_wake_agent_gate(stdout: str) -> bool | None:
    """Parse the final non-empty script line as the optional wake gate."""
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict) and "wakeAgent" in payload:
            return bool(payload["wakeAgent"])
        return None
    return None
