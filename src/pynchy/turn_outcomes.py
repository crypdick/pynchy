"""Explicit outcomes shared by every serialized agent turn."""

from enum import StrEnum


class TurnOutcome(StrEnum):
    """One exhaustive result from a serialized agent turn."""

    COMPLETED = "completed"
    RETRY = "retry_requested"
    CONTINUE_AFTER_SAFE_INTERRUPT = "continue_after_safe_interrupt"
    PAUSED = "paused"
    RESET = "reset"
