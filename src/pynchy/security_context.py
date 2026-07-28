"""Sanitized security evidence provided to policy enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SecurityContextRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class SecurityExecutionAuthorityKind(StrEnum):
    LINEAR_WORK_ITEM_LEASE = "linear_work_item_lease"


@dataclass(frozen=True)
class SecurityContextMessage:
    role: SecurityContextRole
    content: str


@dataclass(frozen=True)
class SecurityExecutionAuthority:
    kind: SecurityExecutionAuthorityKind
    work_item_identifier: str


@dataclass(frozen=True)
class RecentSecurityContext:
    current_user_intent: str | None
    recent_messages: tuple[SecurityContextMessage, ...]
    recent_agent_updates: tuple[str, ...]
    completed_tool_actions: tuple[str, ...]
    execution_authority: SecurityExecutionAuthority | None
