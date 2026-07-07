"""IPC protocol definitions — signal format and validation.

Tier 1 signals carry no payload; the host derives behavior from which
group sent the signal and from its own state.

Tier 2 requests carry a payload with a request_id for response tracking.
They will be routed through Deputy mediation in a future step.

See: backlog/2-planning/security-hardening-0-ipc-surface.md
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pynchy.types import ChatJid, ContainerConfig, GroupFolder

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
        "service:list_calendar",
        "service:create_event",
        "service:delete_event",
        # Slack token extraction
        "service:refresh_slack_tokens",
        "service:setup_slack_session",
        # X (Twitter) integration
        "service:setup_x_session",
        "service:x_post",
        "service:x_like",
        "service:x_reply",
        "service:x_retweet",
        "service:x_quote",
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

    return cast(str, signal)


def parse_ipc_file(file_path: Path) -> dict[str, Any]:
    """Read and parse a JSON IPC file.

    Returns the parsed data dict.
    Raises json.JSONDecodeError or OSError on failure.
    """
    return cast("dict[str, Any]", json.loads(file_path.read_text()))


def make_signal(signal_type: str) -> dict[str, str]:
    """Create a Tier 1 signal payload (for container-side use).

    This is the canonical format for signal-only IPC files.
    """
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(f"Not a valid signal type: {signal_type!r}")
    return {"signal": signal_type}


# --- Typed request models (parse, don't validate) ---
#
# The data-carrying IPC types below reach their handlers as raw ``dict[str, Any]``
# via ``dispatch()``. These models coerce that dict into a constrained shape once,
# at the handler boundary, so the handler body works with typed attributes instead
# of re-extracting and re-checking fields with ``.get()`` / membership tests.
# ``from_dict`` returns ``None`` when a required field is absent, letting the
# handler fail fast on a malformed request.
#
# TODO: only ``register_group``, ``create_periodic_agent`` and the inbound
# ``message`` file are modelled here. The remaining TIER2_TYPES — ``schedule_task``,
# ``schedule_host_job``, ``deploy``, the lifecycle types (``reset_context``,
# ``finished_work``, ``sync_worktree_to_main``, ``pause_task``/``resume_task``/
# ``cancel_task``) and the ``service:*`` / ``service:x_*`` families — still flow to
# their handlers as raw dicts. Model each here as its handler is hardened; they were
# left untyped in this pass to keep the change proportional.


@dataclass(frozen=True)
class RegisterGroupRequest:
    """A validated ``register_group`` request."""

    jid: ChatJid
    name: str
    folder: GroupFolder
    trigger: str
    container_config: ContainerConfig | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegisterGroupRequest | None:
        jid = data.get("jid")
        name = data.get("name")
        folder = data.get("folder")
        trigger = data.get("trigger")
        if not (jid and name and folder and trigger):
            return None
        raw_config = data.get("containerConfig")
        return cls(
            jid=ChatJid(jid),
            name=name,
            folder=GroupFolder(folder),
            trigger=trigger,
            container_config=ContainerConfig.from_dict(raw_config) if raw_config else None,
        )


@dataclass(frozen=True)
class CreatePeriodicAgentRequest:
    """A validated ``create_periodic_agent`` request.

    Cron validity of ``schedule`` is checked by the handler (which owns the
    ``croniter`` dependency and the distinct log message), not here.
    """

    name: str
    schedule: str
    prompt: str
    context_mode: Literal["group", "isolated"]
    claude_md: str
    chat: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreatePeriodicAgentRequest | None:
        name = data.get("name")
        schedule = data.get("schedule")
        prompt = data.get("prompt")
        if not name or not schedule or not prompt:
            return None
        context_mode = data.get("context_mode", "group")
        if context_mode not in ("group", "isolated"):
            context_mode = "group"
        return cls(
            name=name,
            schedule=schedule,
            prompt=prompt,
            context_mode=cast('Literal["group", "isolated"]', context_mode),
            claude_md=data.get("claude_md", f"You are the {name} periodic agent."),
            chat=data.get("chat"),
        )


@dataclass(frozen=True)
class InboundChatMessage:
    """A ``type: message`` IPC file relaying text from a container to a chat."""

    chat_jid: ChatJid
    text: str
    sender: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InboundChatMessage | None:
        if data.get("type") != "message":
            return None
        chat_jid = data.get("chatJid")
        text = data.get("text")
        if not chat_jid or not text:
            return None
        sender = data.get("sender")
        return cls(
            chat_jid=ChatJid(chat_jid),
            text=text,
            sender=sender if isinstance(sender, str) else None,
        )
