"""Startup, first-run setup, and deploy continuation helpers for the main app."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pynchy.config import get_settings
from pynchy.host.git_ops.utils import get_head_commit_message, get_head_sha, is_repo_dirty, run_git
from pynchy.host.migration_backups import prune_migration_backups
from pynchy.logger import logger
from pynchy.state import get_active_task_for_group, get_messages_since
from pynchy.types import WorkspaceProfile, WorkspaceSecurity
from pynchy.utils import write_json_atomic

if TYPE_CHECKING:
    from pynchy.host.orchestrator.concurrency import GroupQueue


@runtime_checkable
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

    async def start_interactive_turn(self, chat_jid: str) -> None: ...

    async def _register_workspace(self, profile: WorkspaceProfile) -> None: ...


async def send_boot_notification(deps: StartupDeps) -> None:
    """Send a system message to the admin channel on startup."""
    s = get_settings()
    from pynchy.host.orchestrator.adapters import find_admin_jid

    admin_jid = find_admin_jid(deps.workspaces) or None
    if not admin_jid:
        return

    sha = get_head_sha()[:8]
    commit_msg = get_head_commit_message(50)
    dirty = " (dirty)" if is_repo_dirty() else ""
    label = f"{sha}{dirty} {commit_msg}".strip() if commit_msg else f"{sha}{dirty}"
    parts = [f"🦞 online -- {label}"]

    # Check for API credentials and warn if missing
    from pynchy.host.container_manager.credentials import has_api_credentials

    if not has_api_credentials():
        parts.append(
            "WARNING: No API credentials found -- messages will fail. "
            "Run 'claude' to authenticate or set ANTHROPIC_API_KEY in config.toml."
        )
        logger.warning("No API credentials found at startup")

    # Check for boot warnings left by the deploy step
    boot_warnings_path = s.data_dir / "boot_warnings.json"
    if boot_warnings_path.exists():
        try:
            warnings = json.loads(boot_warnings_path.read_text(encoding="utf-8"))
            boot_warnings_path.unlink()
            parts.extend(f"WARNING: {warning}" for warning in warnings)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read boot warnings", err=str(exc))
            boot_warnings_path.unlink(missing_ok=True)

    await deps.broadcast_host_message(admin_jid, "\n".join(parts))
    logger.info("Boot notification sent")


async def recover_pending_messages(deps: StartupDeps) -> None:
    """Startup recovery: check for unprocessed messages in registered groups."""
    for chat_jid, group in deps.workspaces.items():
        # Scheduled workspaces run through the task scheduler, not recovery.
        # Without this guard, any stale is_from_me=0 message triggers an
        # agent run via the message handler path.  If that run commits and
        # pushes (e.g. code-improver), sync_poll detects HEAD drift and
        # deploys, sending SIGTERM before the message handler can advance
        # last_agent_timestamp.  On restart, recovery finds the same
        # message again → infinite restart loop.
        task = await get_active_task_for_group(group.folder)
        if task is not None and task.schedule_type == "cron":
            logger.debug(
                "Skipping recovery for scheduled workspace",
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
            await deps.start_interactive_turn(chat_jid)


async def auto_rollback(continuation_path: Path, exc: Exception) -> None:
    """Roll back to the pre-deploy commit if startup fails after a deploy."""
    try:
        continuation = json.loads(continuation_path.read_text(encoding="utf-8"))
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
        continuation = json.loads(continuation_path.read_text(encoding="utf-8"))
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
    _prune_migration_backups(get_settings().data_dir)

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

    for jid in active_sessions:
        # Active session existed before deploy → send as system notice
        # (visible to both user and LLM). broadcast_system_notice stores
        # the message, broadcasts to channels, and enqueues a message check.
        #
        # Note: periodic workspaces are NOT skipped here. The active_sessions
        # dict already filters to workspaces that were running at deploy time.
        # Idle periodic workspaces have no session (cleared on completion),
        # so they won't appear here. Only interrupted tasks need resuming.
        notice = f"Deploy complete -- {label}. {resume_prompt}"
        await deps.broadcast_system_notice(jid, notice)
        await deps.start_interactive_turn(jid)
        logger.info("Deploy resume notice sent", chat_jid=jid)


def _prune_migration_backups(data_dir: Path) -> None:
    backups_dir = data_dir / "migration-backups"
    try:
        result = prune_migration_backups(backups_dir)
    except OSError as exc:
        logger.warning(
            "Failed to prune migration backups",
            path=str(backups_dir),
            err=str(exc),
        )
        return

    if result.removed:
        logger.info(
            "Pruned migration backups",
            path=str(backups_dir),
            removed_count=len(result.removed),
            kept_count=len(result.kept),
        )


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
    # Non-admin workspaces get security gating via the human-approval gate
    # (see security/approval.py and WorkspaceSecurity in config_models.py).
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
    return [cred for cred in required if cred not in os.environ]
