"""Shared models for the Obsidian learning queue."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class LearningQueueError(RuntimeError):
    """Raised when queue state changes would violate durable ownership."""


@dataclass(frozen=True)
class LearningPacket:
    job_id: str
    chat_jid: str
    group_folder: str
    profile: str
    created_at: str
    messages: list[dict[str, str]]
    final_answer: str | None
    tool_counts: dict[str, int]
    error_snippets: list[str]
    loaded_skills: list[str]
    provenance: dict[str, str]
    attempts: int = 0


@dataclass(frozen=True)
class ClaimedLearningPacket:
    packet: LearningPacket
    path: Path
    claim_id: str
