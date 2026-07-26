"""Scheduled occurrence policy for thread-owned durable sessions."""

from enum import StrEnum


class SessionPolicy(StrEnum):
    """How a scheduled occurrence treats its thread-owned durable session."""

    CONTINUE = "continue"
    RESET_BEFORE_RUN = "reset_before_run"
