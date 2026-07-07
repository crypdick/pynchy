"""Shared test helpers for the durable Obsidian learning queue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pynchy.host.learning.queue import LearningPacket


def packet(job_id: str = "job-1") -> LearningPacket:
    return LearningPacket(
        job_id=job_id,
        chat_jid="slack:C123",
        group_folder="shopping",
        profile="default",
        created_at="2026-07-07T10:00:00+00:00",
        messages=[{"role": "user", "content": "remember the milk"}],
        final_answer="Added milk to the list.",
        tool_counts={"shell": 1},
        error_snippets=["temporary model error"],
        loaded_skills=["shopping-list"],
        provenance={"run_id": "run-123"},
    )


def base_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ipc" / "learning"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())
