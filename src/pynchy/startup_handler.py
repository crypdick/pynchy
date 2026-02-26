"""Startup, first-run setup, and deploy continuation helpers for the main app."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.config import get_settings
from pynchy.db import get_messages_since
from pynchy.git_ops.utils import get_head_commit_message, get_head_sha, is_repo_dirty, run_git
from pynchy.ipc._write import write_json_atomic
from pynchy.logger import logger
from pynchy.types import WorkspaceProfile, WorkspaceSecurity

if TYPE_CHECKING:
    from pynchy.group_queue import GroupQueue


class StartupDeps(Protocol):
    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def channels(self) -> list[Any]: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None: ...

    async def _register_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def register_jid_alias(
        self, alias_jid: str, canonical_jid: str, channel_name: str
    ) -> None: ...


async def send_boot_notification(deps: StartupDeps) -> None:
    """Send a system message to the admin channel on startup."""
    s = get_settings()
    from pynchy.adapters import find_admin_jid

    admin_jid = find_admin_jid(deps.workspaces) or None
    if not admin_jid:
        return

    sha = get_head_sha()[:8]
    commit_msg = get_head_commit_message(50)
    dirty = " (dirty)" if is_repo_dirty() else ""
    label = f"{sha}{dirty} {commit_msg}".strip() if commit_msg else f"{sha}{dirty}"
    parts = [f"🦞 online -- {label}"]

    # Check for API credentials and warn if missing
    from pynchy.container_runner._credentials import has_api_credentials

    if not has_api_credentials():
        parts.append(
            "WARNING: No API credentials found -- messages will fail. "
            "Run 'claude' to authenticate or set ANTHROPIC_API_KEY in config.toml."
        )
        logger.warning("No API credentials found at startup")

    # Check for boot warnings left by a previous deploy
    boot_warnings_path = s.data_dir / "boot_warnings.json"
    if boot_warnings_path.exists():
        try:
            warnings = json.loads(boot_warnings_path.read_text())
            boot_warnings_path.unlink()
            for warning in warnings:
                parts.append(f"WARNING: {warning}")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read boot warnings", err=str(exc))
            boot_warnings_path.unlink(missing_ok=True)

    await deps.broadcast_host_message(admin_jid, "\n".join(parts))
    logger.info("Boot notification sent")


async def recover_pending_messages(deps: StartupDeps) -> None:
    """Startup recovery: check for unprocessed messages in registered groups."""
    from pynchy.workspace_config import load_workspace_config

    for chat_jid, group in deps.workspaces.items():
        # Skip periodic (scheduled) workspaces — they run on their own
        # schedule via the task scheduler, not through message recovery.
        # Without this guard, any stale is_from_me=0 message triggers an
        # agent run via the message handler path.  If that run commits and
        # pushes (e.g. code-improver), sync_poll detects HEAD drift and
        # deploys, sending SIGTERM before the message handler can advance
        # last_agent_timestamp.  On restart, recovery finds the same
        # message again → infinite restart loop.
        ws_config = load_workspace_config(group.folder)
        if ws_config and ws_config.is_periodic:
            logger.debug(
                "Skipping recovery for periodic workspace",
                chat_jid=chat_jid,
                group=group.folder,
            )
            continue

        since_timestamp = deps.last_agent_timestamp.get(chat_jid, "")
        pending = await get_messages_since(chat_jid, since_timestamp)
        if pending:
            logger.info(
                "Recovery: found unprocessed messages",
                group=group.name,
                pending_count=len(pending),
            )
            deps.queue.enqueue_message_check(chat_jid)


async def auto_rollback(continuation_path: Path, exc: Exception) -> None:
    """Roll back to the previous commit if startup fails after a deploy."""
    try:
        continuation = json.loads(continuation_path.read_text())
    except (json.JSONDecodeError, OSError) as read_exc:
        logger.exception(
            "Failed to read continuation for rollback",
            path=str(continuation_path),
            error=str(read_exc),
        )
        return

    previous_sha = continuation.get("previous_commit_sha", "")
    if not previous_sha:
        logger.warning("No previous_commit_sha in continuation, cannot rollback")
        return

    logger.warning(
        "Startup failed after deploy, rolling back",
        previous_sha=previous_sha,
        error=str(exc),
    )

    result = run_git("reset", "--hard", previous_sha)
    if result.returncode != 0:
        logger.error("Rollback git reset failed", stderr=result.stderr)
        return

    # Rewrite continuation with rollback info (clear previous_commit_sha to prevent loops)
    error_short = str(exc)[:200]
    continuation["resume_prompt"] = (
        f"ROLLBACK: Startup failed ({error_short}). Rolled back to {previous_sha[:8]}."
    )
    continuation["previous_commit_sha"] = ""
    write_json_atomic(continuation_path, continuation, indent=2)

    logger.info("Rollback complete, exiting for service restart")
    sys.exit(1)


async def check_deploy_continuation(deps: StartupDeps) -> None:
    """Check for a deploy continuation file and resume active sessions.

    Reads the ``active_sessions`` dict from the continuation file and sends
    a system notice (visible to both user and LLM) for every group that had
    an active session before the deploy.
    """
    continuation_path = get_settings().data_dir / "deploy_continuation.json"
    if not continuation_path.exists():
        return

    try:
        continuation = json.loads(continuation_path.read_text())
        continuation_path.unlink()
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "Failed to read deploy continuation",
            path=str(continuation_path),
            err=str(exc),
        )
        return

    resume_prompt = continuation.get("resume_prompt", "Deploy complete.")
    commit_sha = continuation.get("commit_sha", "unknown")

    active_sessions: dict[str, str] = continuation.get("active_sessions", {})

    if not active_sessions:
        logger.info(
            "Deploy continuation has no active sessions, skipping agent resume",
            commit_sha=commit_sha,
        )
        return

    logger.info(
        "Deploy continuation found, resuming sessions",
        commit_sha=commit_sha,
        group_count=len(active_sessions),
    )

    sha_short = commit_sha[:8]
    commit_msg = get_head_commit_message(50)
    label = f"{sha_short} {commit_msg}".strip() if commit_msg else sha_short

    from pynchy.workspace_config import load_workspace_config

    for jid, _session_id in active_sessions.items():
        # Skip periodic (scheduled) workspaces — they don't need deploy
        # resumption since they'll run at their next scheduled time.
        # Without this guard, every deploy injects a user-visible message
        # that triggers a full agent run (burning tokens for no reason).
        group = deps.workspaces.get(jid)
        if group:
            ws_config = load_workspace_config(group.folder)
            if ws_config and ws_config.is_periodic:
                logger.debug(
                    "Skipping deploy resume for periodic workspace",
                    chat_jid=jid,
                    group=group.folder,
                )
                continue

        # Active session existed before deploy → send as system notice
        # (visible to both user and LLM). broadcast_system_notice stores
        # the message, broadcasts to channels, and enqueues a message check.
        notice = f"Deploy complete -- {label}. {resume_prompt}"
        await deps.broadcast_system_notice(jid, notice)
        deps.queue.enqueue_message_check(jid)
        logger.info("Deploy resume notice sent", chat_jid=jid)


# ------------------------------------------------------------------
# First-run setup
# ------------------------------------------------------------------


async def setup_admin_group(deps: StartupDeps, default_channel: Any | None) -> None:
    """Create and register the first admin workspace.

    If a default channel with ``create_group`` is available, provision a
    channel-native group. Otherwise bootstrap a local TUI workspace so core
    usage is never coupled to external channels.
    """
    s = get_settings()
    group_name = s.agent.name.title()
    logger.info("No groups registered. Creating first admin workspace...", name=group_name)

    jid = f"tui://{s.agent.name}"
    if default_channel and hasattr(default_channel, "create_group"):
        jid = await default_channel.create_group(group_name)
        logger.info(
            "Created first-run group via channel",
            channel=default_channel.name,
            jid=jid,
        )
    else:
        logger.info("No channel group support found, creating TUI local workspace", jid=jid)

    # Create admin workspace with permissive security profile.
    # Admin group is fully trusted — auto-approve all tools.
    # TODO: Re-evaluate when human-approval gate is implemented (see
    #   backlog/2-planning/security-hardening-6-approval.md). At that point,
    #   consider keeping admin at always-approve but requiring approval for
    #   non-admin workspaces' destructive actions.
    profile = WorkspaceProfile(
        jid=jid,
        name=group_name,
        folder=s.agent.name,
        trigger=f"@{s.agent.name}",
        added_at=datetime.now(UTC).isoformat(),
        is_admin=True,
        # Admin workspace: no service-level gating needed — fully trusted.
        security=WorkspaceSecurity(),
    )
    await deps._register_workspace(profile)
    logger.info("Admin workspace created", group=group_name, jid=jid)


def validate_plugin_credentials(plugin: Any) -> list[str]:
    """Check if plugin has required environment variables.

    Args:
        plugin: Plugin instance with optional requires_credentials() method

    Returns:
        List of missing credential names (empty if all present)
    """
    if not hasattr(plugin, "requires_credentials"):
        return []

    required = plugin.requires_credentials()
    missing = [cred for cred in required if cred not in os.environ]
    return missing
