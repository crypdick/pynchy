"""Runtime values for configured message channels."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SlackConnectionSettings:
    """Resolved values required to construct one Slack channel."""

    bot_token_env: str
    app_token_env: str
    chat_names: tuple[str, ...]
    assistant_name: str
    allow_create: bool


@dataclass(frozen=True)
class WhatsAppConnectionSettings:
    """Resolved values required to construct one WhatsApp channel."""

    auth_db_path: Path
    assistant_name: str
