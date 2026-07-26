"""Explicit outcomes shared by message processing and its recovery paths."""

from __future__ import annotations


class ContinueAfterSafeInterrupt:
    """Signal that the next pending message needs a fresh Temporal activity."""


class TurnPaused:
    """Signal terminal hibernation with an unfinished resumable checkpoint."""


class TurnReset:
    """Signal terminal cancellation because the checkpoint was discarded."""


CONTINUE_AFTER_SAFE_INTERRUPT = ContinueAfterSafeInterrupt()
TURN_PAUSED = TurnPaused()
TURN_RESET = TurnReset()
type ProcessGroupResult = bool | ContinueAfterSafeInterrupt | TurnPaused | TurnReset
