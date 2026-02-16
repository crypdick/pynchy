"""IPC protocol definitions — signal format and validation.

Tier 1 signals carry no payload; the host derives behavior from which
group sent the signal and from its own state.

Tier 2 requests carry a payload with a request_id for response tracking.
They will be routed through Deputy mediation in a future step.

See: backlog/2-planning/security-hardening-0-ipc-surface.md
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Tier 1: Signal-only IPC types (no payload crosses the boundary)
SIGNAL_TYPES = frozenset(
    {
        "refresh_groups",
        # Future: "context_reset", "message_ready", "progress_ready"
    }
)

# Tier 2: Data-carrying IPC types (Deputy mediation planned)
TIER2_TYPES = frozenset(
    {
        "schedule_task",
        "schedule_host_job",
        "deploy",
        "register_group",
        "create_periodic_agent",
        # Lifecycle: still carries data, will be reviewed later
        "reset_context",
        "finished_work",
        "sync_worktree_to_main",
        # Task management
        "pause_task",
        "resume_task",
        "cancel_task",
        # Service requests (policy-gated, Step 2)
        "service:read_email",
        "service:send_email",
        "service:list_calendar",
        "service:create_event",
        "service:delete_event",
        "service:search_passwords",
        "service:get_password",
    }
)


def validate_signal(data: dict[str, Any]) -> str | None:
    """Check if data is a valid Tier 1 signal.

    Returns the signal type if valid, None if it's not a signal
    (i.e. it's a Tier 2 data-carrying request).

    Raises ValueError if the file claims to be a signal but is malformed.
    """
    signal = data.get("signal")
    if signal is None:
        return None

    if signal not in SIGNAL_TYPES:
        raise ValueError(f"Unknown signal type: {signal!r}")

    # Signals must not carry payload data beyond the signal field itself
    extra_keys = set(data.keys()) - {"signal", "timestamp"}
    if extra_keys:
        raise ValueError(
            f"Signal {signal!r} contains unexpected payload keys: {extra_keys}. "
            "Signals must be payload-free."
        )

    return signal


def parse_ipc_file(file_path: Path) -> dict[str, Any]:
    """Read and parse a JSON IPC file.

    Returns the parsed data dict.
    Raises json.JSONDecodeError or OSError on failure.
    """
    return json.loads(file_path.read_text())


def make_signal(signal_type: str) -> dict[str, str]:
    """Create a Tier 1 signal payload (for container-side use).

    This is the canonical format for signal-only IPC files.
    """
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(f"Not a valid signal type: {signal_type!r}")
    return {"signal": signal_type}
