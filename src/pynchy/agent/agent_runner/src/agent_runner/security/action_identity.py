"""Per-tool guarded-action identity shared by the built-in hook chain."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, NewType

if TYPE_CHECKING:
    from collections.abc import Iterator

GuardedActionId = NewType("GuardedActionId", str)

_current_action_id: ContextVar[GuardedActionId | None] = ContextVar(
    "pynchy_guarded_action_id", default=None
)


@contextmanager
def guarded_action_scope() -> Iterator[None]:
    """Give all built-in hooks for one tool call the same correlation ID."""
    token = _current_action_id.set(GuardedActionId(uuid.uuid4().hex))
    try:
        yield
    finally:
        _current_action_id.reset(token)


def current_guarded_action_id() -> GuardedActionId:
    """Return the active ID, or an independent ID for direct hook calls."""
    return _current_action_id.get() or GuardedActionId(uuid.uuid4().hex)
