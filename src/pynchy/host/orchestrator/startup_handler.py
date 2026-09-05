"""Startup, first-run setup, and deploy continuation helpers for the main app."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves startup runtime annotations.
)
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - beartype resolves startup annotations at runtime.
from typing import TYPE_CHECKING, NoReturn, Protocol, runtime_checkable

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
)
from pynchy.atomic_json import write_json_atomic
from pynchy.deployments import DeployRevision
from pynchy.host.orchestrator import adapters, session_handler
from pynchy.host.orchestrator.deploy import rollback_checkout
from pynchy.host.orchestrator.messaging.sender_policy import (
    SenderPolicyDeps,
    load_allowed_group_messages,
)
from pynchy.host.orchestrator.startup_rollback import (
    ensure_rollback_evidence_durable,
    terminate_failed_startup,
)
from pynchy.logger import logger
from pynchy.state.api import (
    clear_in_flight_turn,
    clear_pending_deployment,
    complete_deployment,
    get_active_task_for_group,
    get_deployment_state,
    get_in_flight_turns,
    get_router_state,
    prepare_conversation_delivery_recovery,
    prepare_in_flight_turn_recovery,
    set_chat_cleared_at,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
    WorkspaceSecurity,
)

if TYPE_CHECKING:
    from pynchy.host.orchestrator.concurrency import GroupQueue


class _NotificationsConfig(Protocol):
    admin_workspace: str | None


class _AgentConfig(Protocol):
    name: str


class StartupSettings(Protocol):
    data_dir: Path
    notifications: _NotificationsConfig
    agent: _AgentConfig


class _GitResult(Protocol):
    returncode: int
    stderr: str


def _unconfigured_settings() -> StartupSettings:
    raise RuntimeError("Startup configuration has not been composed")


def _unconfigured_git(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("Startup Git operations have not been composed")


_get_settings: Callable[[], StartupSettings] = _unconfigured_settings
get_head_commit_message: Callable[[int], str] = _unconfigured_git
get_head_sha: Callable[[], str] = _unconfigured_git
is_repo_dirty: Callable[[], bool] = _unconfigured_git
run_git: Callable[..., _GitResult] = _unconfigured_git


@dataclass(frozen=True)
class StartupRuntime:
    get_settings: Callable[[], StartupSettings]
    head_commit_message: Callable[[int], str]
    head_sha: Callable[[], str]
    repo_dirty: Callable[[], bool]
    git: Callable[..., _GitResult]


def configure_startup_runtime(runtime: StartupRuntime) -> None:
    """Bind startup configuration and source-control operations at composition."""
    global _get_settings, get_head_commit_message, get_head_sha  # noqa: PLW0603 - one host process owns startup operations.
    global is_repo_dirty, run_git  # noqa: PLW0603 - one host process owns startup operations.
    _get_settings = runtime.get_settings
    get_head_commit_message = runtime.head_commit_message
    get_head_sha = runtime.head_sha
    is_repo_dirty = runtime.repo_dirty
    run_git = runtime.git


def get_settings() -> StartupSettings:
    return _get_settings()


_DEPLOY_CONTINUATION_NAME = "deploy_continuation.json"
_CLAIMED_DEPLOY_CONTINUATION_NAME = "deploy_continuation.startup.json"


@runtime_checkable
class StartupDeps(SenderPolicyDeps, Protocol):
    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def session_cleared(self) -> set[str]: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def start_interactive_turn(self, chat_jid: str) -> None: ...

    async def start_interrupted_turn(self, turn_id: str, group_folder: str) -> None: ...

    async def register_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def prepare_context_reset(self, group: WorkspaceProfile) -> None: ...

    async def destroy_runtime_session(self, group_folder: str) -> None: ...

    def has_api_credentials(self) -> bool: ...


async def send_boot_notification(deps: StartupDeps) -> None:
    """Send a system message to the admin channel on startup."""
    s = get_settings()

    admin_jid = (
        adapters.resolve_admin_notification_jid(deps.workspaces, s.notifications.admin_workspace)
        or None
    )
    if not admin_jid:
        return

    sha = get_head_sha()[:8]
    commit_msg = get_head_commit_message(50)
    dirty = " (dirty)" if is_repo_dirty() else ""
    label = f"{sha}{dirty} {commit_msg}".strip() if commit_msg else f"{sha}{dirty}"
    parts = [f"🦞 online -- {label}"]

    # Check for API credentials and warn if missing

    if not deps.has_api_credentials():
        parts.append(
            "WARNING: No API credentials found -- messages will fail. "
            "Run 'claude' to authenticate or set ANTHROPIC_API_KEY in .env."
        )
        logger.warning("No API credentials found at startup")

    # Check for boot warnings left by the deploy step
    boot_warnings_path = s.data_dir / "boot_warnings.json"
    if await asyncio.to_thread(boot_warnings_path.exists):
        try:
            warnings_text = await asyncio.to_thread(boot_warnings_path.read_text, encoding="utf-8")
            warnings = json.loads(warnings_text)
            await asyncio.to_thread(boot_warnings_path.unlink)
            parts.extend(f"WARNING: {warning}" for warning in warnings)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read boot warnings", err=str(exc))
            boot_warnings_path.unlink(missing_ok=True)

    await deps.broadcast_host_message(admin_jid, "\n".join(parts))
    logger.info("Boot notification sent")


async def recover_pending_messages(
    deps: StartupDeps,
    *,
    exclude_chat_jids: set[str] | None = None,
) -> None:
    """Startup recovery: check for unprocessed messages in registered groups."""
    excluded = exclude_chat_jids or set()
    for chat_jid, group in deps.workspaces.items():
        if chat_jid in excluded:
            continue
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
        pending = await load_allowed_group_messages(
            deps,
            chat_jid,
            group,
            since_timestamp,
        )
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
        continuation_text = await asyncio.to_thread(continuation_path.read_text, encoding="utf-8")
        continuation = json.loads(continuation_text)
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

    rollback = rollback_checkout(
        previous_sha,
        get_head_sha=get_head_sha,
        run_git=run_git,
    )
    if not rollback.success:
        logger.error("Rollback git reset failed", error=rollback.error)
        return

    # Rewrite continuation with rollback info (clear previous_commit_sha to prevent loops)
    error_short = str(exc)[:200]
    continuation["resume_prompt"] = (
        f"ROLLBACK: Startup failed ({error_short}). Rolled back to {previous_sha[:8]}."
    )
    continuation["previous_commit_sha"] = ""
    continuation["rolled_back"] = True
    write_json_atomic(continuation_path, continuation, indent=2)

    attempted_sha = str(continuation.get("commit_sha", "unknown"))
    boot_warning_path = continuation_path.parent / "boot_warnings.json"
    try:
        warnings = json.loads(boot_warning_path.read_text()) if boot_warning_path.exists() else []
    except (OSError, json.JSONDecodeError) as warning_exc:
        logger.warning(
            "Failed to read boot warnings while recording rollback",
            err=str(warning_exc),
        )
        warnings = []
    warnings.append(
        f"Auto-deploy {attempted_sha} failed during startup: {type(exc).__name__}: {error_short}. "
        f"Rolled back to {previous_sha}. Server health: healthy (recovered after rollback)."
    )
    write_json_atomic(boot_warning_path, warnings)
    await asyncio.to_thread(
        ensure_rollback_evidence_durable,
        continuation_path,
        boot_warning_path,
    )

    logger.info("Rollback complete, exiting for service restart")
    terminate_failed_startup()


def _read_deploy_continuation(continuation_path: Path) -> dict[str, object]:
    parsed = json.loads(continuation_path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict):
        return parsed
    logger.error(
        "Deploy continuation must contain a JSON object",
        path=str(continuation_path),
    )
    return {}


def claim_deploy_continuation(data_dir: Path) -> Path:
    """Atomically claim the newest continuation generation for this startup."""
    canonical = data_dir / _DEPLOY_CONTINUATION_NAME
    claimed = data_dir / _CLAIMED_DEPLOY_CONTINUATION_NAME
    if canonical.exists():
        canonical.replace(claimed)
    return claimed


@dataclass(frozen=True)
class InterruptedTurnRecovery:
    """Startup recovery data prepared before the Temporal worker can claim work."""

    turns: tuple[InFlightTurn, ...]
    commit_sha: str
    resume_prompt: str
    had_deploy_continuation: bool
    deploy_revision: DeployRevision | None
    rolled_back: bool
    continuation_path: Path | None


async def prepare_interrupted_turn_recovery(
    deps: StartupDeps | None = None,
    *,
    continuation_path: Path,
) -> InterruptedTurnRecovery:
    """Clear stale claims before Temporal can redeliver interrupted activities."""
    continuation: dict[str, object] = {}
    loaded_continuation_path: Path | None = None
    if await asyncio.to_thread(continuation_path.exists):
        try:
            continuation = _read_deploy_continuation(continuation_path)
            loaded_continuation_path = continuation_path
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Failed to read deploy continuation",
                path=str(continuation_path),
                err=str(exc),
            )

    default_prompt = "Deploy complete." if continuation else "Continuing after host restart."
    resume_prompt = continuation.get("resume_prompt", default_prompt)
    commit_sha = continuation.get("commit_sha", "unknown")
    config_hash = continuation.get("config_hash")
    deploy_id = commit_sha if isinstance(commit_sha, str) and commit_sha != "unknown" else None
    await _complete_reset_requests_after_restart(deps)
    await prepare_conversation_delivery_recovery()
    interrupted_turns = await prepare_in_flight_turn_recovery(deploy_id)
    commit_text = commit_sha if isinstance(commit_sha, str) else "unknown"
    prompt_text = resume_prompt if isinstance(resume_prompt, str) else default_prompt
    deploy_revision = (
        DeployRevision(commit_text, config_hash)
        if commit_text != "unknown" and isinstance(config_hash, str) and config_hash
        else None
    )
    return InterruptedTurnRecovery(
        turns=tuple(interrupted_turns),
        commit_sha=commit_text,
        resume_prompt=prompt_text,
        had_deploy_continuation=bool(continuation),
        deploy_revision=deploy_revision,
        rolled_back=continuation.get("rolled_back") is True,
        continuation_path=loaded_continuation_path,
    )


async def _complete_reset_requests_after_restart(deps: StartupDeps | None) -> None:
    """Finish reset transitions that lost their in-process command handler."""
    reset_at = datetime.now(UTC).isoformat()
    for turn in await get_in_flight_turns():
        if turn.control_state is not CheckpointControlState.RESET_REQUESTED:
            continue
        if deps is None:
            raise RuntimeError("Startup reset requires initialized lifecycle dependencies")
        group = next(
            (
                workspace
                for workspace in deps.workspaces.values()
                if workspace.folder == turn.group_folder
            ),
            None,
        )
        if group is None:
            raise RuntimeError(f"Reset runtime no longer exists: {turn.group_folder}")
        await session_handler.clear_durable_context(deps, group)
        await clear_in_flight_turn(turn.turn_id)
        await set_chat_cleared_at(group.jid, reset_at)
        logger.info(
            "Startup completed checkpoint reset",
            chat_jid=group.jid,
            turn_id=turn.turn_id,
        )


async def resolve_deploy_startup(
    recovery: InterruptedTurnRecovery,
    *,
    active_revision: DeployRevision,
) -> None:
    """Resolve durable deployment state while rollback evidence remains claimed."""
    revision = recovery.deploy_revision
    if revision is not None:
        # A newer external release can start while an older turn continuation remains.
        # Runtime image is authoritative; publishing the stale revision causes rollback loops.
        if recovery.rolled_back or revision != active_revision:
            await clear_pending_deployment(revision)
            await complete_deployment(active_revision)
        else:
            await complete_deployment(revision)
        return

    deployment_state = await get_deployment_state()
    last_deploy_sha = await get_router_state("last_deploy_sha")
    if deployment_state.applied != active_revision or last_deploy_sha != active_revision.commit_sha:
        await complete_deployment(active_revision)


async def finalize_deploy_startup(recovery: InterruptedTurnRecovery) -> None:
    """Retire only the continuation generation claimed by this startup."""
    if recovery.continuation_path is not None:
        await asyncio.to_thread(recovery.continuation_path.unlink, missing_ok=True)


async def confirm_deploy_startup(
    recovery: InterruptedTurnRecovery,
    *,
    active_revision: DeployRevision,
) -> None:
    """Resolve deployment state and retire this startup's continuation."""
    await resolve_deploy_startup(recovery, active_revision=active_revision)
    await finalize_deploy_startup(recovery)


async def dispatch_interrupted_turn_recovery(
    deps: StartupDeps,
    recovery: InterruptedTurnRecovery,
) -> set[str]:
    """Dispatch prepared rows after the Temporal worker is ready to receive them."""
    interrupted_turns = recovery.turns

    if not interrupted_turns:
        logger.info(
            "Startup recovery has no interrupted agent turns",
            commit_sha=recovery.commit_sha,
        )
        return set()

    logger.info(
        "Startup recovery found interrupted agent turns",
        commit_sha=recovery.commit_sha,
        turn_count=len(interrupted_turns),
    )

    sha_short = recovery.commit_sha[:8]
    commit_msg = get_head_commit_message(50) if recovery.had_deploy_continuation else ""
    label = f"{sha_short} {commit_msg}".strip() if commit_msg else sha_short
    resumed_chats: set[str] = set()

    for turn in interrupted_turns:
        current = next(
            (
                workspace
                for workspace in deps.workspaces.values()
                if workspace.folder == turn.group_folder
            ),
            None,
        )
        if current is None:
            logger.error(
                "Interrupted turn runtime no longer exists",
                group_folder=turn.group_folder,
                turn_id=turn.turn_id,
            )
            await deps.start_interrupted_turn(turn.turn_id, turn.group_folder)
            continue
        restart_label = (
            f"Deploy complete -- {label}."
            if recovery.had_deploy_continuation
            else "Pynchy restarted."
        )
        notice = f"{restart_label} Resuming interrupted work. {recovery.resume_prompt}"
        await deps.broadcast_host_message(current.jid, notice)
        await deps.start_interrupted_turn(turn.turn_id, turn.group_folder)
        resumed_chats.add(current.jid)
        logger.info(
            "Interrupted turn recovery dispatched",
            chat_jid=current.jid,
            group_folder=turn.group_folder,
            turn_id=turn.turn_id,
            work_kind=turn.work_kind.value,
        )
    return resumed_chats


async def check_deploy_continuation(  # noqa: V103
    deps: StartupDeps,
    *,
    active_revision: DeployRevision,
) -> set[str]:
    """Prepare and dispatch recovery when startup ordering is managed by the caller."""
    continuation_path = claim_deploy_continuation(get_settings().data_dir)
    recovery = await prepare_interrupted_turn_recovery(
        deps,
        continuation_path=continuation_path,
    )
    await confirm_deploy_startup(recovery, active_revision=active_revision)
    return await dispatch_interrupted_turn_recovery(deps, recovery)


# ------------------------------------------------------------------
# First-run setup
# ------------------------------------------------------------------


async def setup_admin_group(deps: StartupDeps, default_channel: object | None) -> None:
    """Create and register the first admin workspace.

    The command-center channel must support group creation. Pynchy does not
    synthesize a workspace without a real channel destination.
    """
    s = get_settings()
    group_name = s.agent.name.title()
    logger.info("No groups registered. Creating first admin workspace...", name=group_name)

    if default_channel is None or not hasattr(default_channel, "create_group"):
        raise RuntimeError(
            "First-run setup requires command_center.connection to select a "
            "channel that supports chat creation"
        )
    jid = await default_channel.create_group(group_name)
    logger.info(
        "Created first-run group via channel",
        channel=default_channel.name,
        jid=jid,
    )

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
    await deps.register_workspace(profile)
    logger.info("Admin workspace created", group=group_name, jid=jid)


def validate_plugin_credentials(plugin: object) -> list[str]:
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
