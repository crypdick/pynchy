"""Value objects for host-owned pull-request publication."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrPublication:
    """Agent-authored review text and the host-selected remote branch."""

    source_label: str
    fallback_title: str
    branch_name: str | None = None
    title: str | None = None
    body: str | None = None
