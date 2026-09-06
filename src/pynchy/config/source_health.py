"""Configuration for Pynchy's host-local messaging health projection."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class MessagingSourceHealthConfig(BaseModel):
    """Host-owned aggregate source metadata exposed to authorized agents."""

    model_config = ConfigDict(extra="forbid")

    data_dir: Path | None = None
    stale_after_hours: int = Field(default=24, ge=1)
