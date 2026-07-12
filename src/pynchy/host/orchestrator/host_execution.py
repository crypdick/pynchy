"""Host execution helpers for direct agent runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config import get_settings
from pynchy.host.container_manager.credentials import build_agent_env_vars
from pynchy.logger import logger

_CODEX_SESSION_PREFIX = "codex:"


def codex_thread_id(session_id: str | None) -> str | None:
    if not session_id or not session_id.startswith(_CODEX_SESSION_PREFIX):
        return None
    body = session_id.removeprefix(_CODEX_SESSION_PREFIX)
    if ":" in body:
        _model, body = body.split(":", maxsplit=1)
    return body or None


def codex_thread_exists_in_host_runtime(session_id: str | None) -> bool:
    thread_id = codex_thread_id(session_id)
    if thread_id is None:
        return True

    index_path = _codex_home() / "session_index.jsonl"
    try:
        lines = index_path.read_text().splitlines()
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("Could not read Codex session index", path=str(index_path))
        return False

    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id") == thread_id:
            return True
    return False


def host_execution_cwd(group_folder: str) -> Path | None:
    resolved = workspace_config.load_resolved_config(group_folder)
    if resolved is None or resolved.execution_mode != "host":
        return None
    if not resolved.cwd:
        return None
    return Path(resolved.cwd).expanduser()


def host_agent_env_vars(*, is_admin: bool, group_folder: str) -> dict[str, str]:
    env = build_agent_env_vars(is_admin=is_admin, group_folder=group_folder)
    for key in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        if key in env:
            env[key] = _host_reachable_gateway_url(env[key])
    return env


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _host_reachable_gateway_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return base_url

    port = parsed.port or get_settings().gateway.port
    return urlunparse(parsed._replace(netloc=f"localhost:{port}"))
