"""Resolved agent instructions for authenticated Linear webhook activity."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LinearWebhookPrompts:
    """Prompt content resolved by the host composition root."""

    issue: str
    comment: str
