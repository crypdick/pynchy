"""Durable self-echo evidence for host-owned Linear mutations."""

from __future__ import annotations

from functools import partial
from typing import Any

from pynchy.plugins.integrations.linear_client import (
    LinearError,
    LinearSelfEchoRecorder,
)
from pynchy.state import (
    LinearCommentSelfEcho,
    LinearIssueStateSelfEcho,
    record_linear_comment_self_echo,
    record_linear_issue_state_self_echo,
)


def linear_self_echo_recorder(account_name: str) -> LinearSelfEchoRecorder:
    """Create callbacks that persist evidence for one Linear account's writes."""
    return LinearSelfEchoRecorder(
        comment_created=partial(_record_linear_comment_self_echo, account_name),
        issue_state_updated=partial(_record_linear_issue_state_self_echo, account_name),
    )


def _self_echo_evidence_text(evidence: dict[str, Any], key: str) -> str:
    value = evidence.get(key)
    if not isinstance(value, str) or not value:
        raise LinearError("Linear provider response lost its self-echo evidence")
    return value


async def _record_linear_comment_self_echo(
    account_name: str,
    comment: dict[str, Any],
) -> None:
    """Records comment self-echo evidence for matching webhook callbacks."""
    await record_linear_comment_self_echo(
        LinearCommentSelfEcho(
            account_name=account_name,
            comment_id=_self_echo_evidence_text(comment, "id"),
            issue_id=_self_echo_evidence_text(comment, "issueId"),
            revision=_self_echo_evidence_text(comment, "updatedAt"),
        )
    )


async def _record_linear_issue_state_self_echo(
    account_name: str,
    issue: dict[str, Any],
) -> None:
    """Records issue-state self-echo evidence for matching webhook callbacks."""
    await record_linear_issue_state_self_echo(
        LinearIssueStateSelfEcho(
            account_name=account_name,
            issue_id=_self_echo_evidence_text(issue, "id"),
            state_id=_self_echo_evidence_text(issue, "stateId"),
            revision=_self_echo_evidence_text(issue, "updatedAt"),
        )
    )
