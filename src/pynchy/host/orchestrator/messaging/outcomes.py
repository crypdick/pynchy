"""Explicit outcomes shared by message processing and its recovery paths."""

from __future__ import annotations


class ContinueAfterSafeInterrupt:
    """Signal that the next pending message needs a fresh Temporal activity."""


CONTINUE_AFTER_SAFE_INTERRUPT = ContinueAfterSafeInterrupt()
type ProcessGroupResult = bool | ContinueAfterSafeInterrupt
