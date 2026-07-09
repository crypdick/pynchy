"""File-based IPC watcher.

Uses watchdog (inotify on Linux, FSEvents on macOS) for event-driven
file processing.  On startup, sweeps existing files for crash recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves IPC watcher paths at runtime.

from watchdog.observers import Observer

from pynchy.config import get_settings
from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # noqa: TC001, RUF100 - beartype resolves IPC watcher deps at runtime.
)
from pynchy.host.container_manager.ipc.events import IpcEventHandler as _IpcEventHandler
from pynchy.host.container_manager.ipc.handlers_signals import handle_signal as _handle_signal
from pynchy.host.container_manager.ipc.ledger import (
    claim_request_for_execution as _claim_request_for_execution,
)
from pynchy.host.container_manager.ipc.output_claims import claim_output_file
from pynchy.host.container_manager.ipc.protocol import (
    InboundChatMessage,
    parse_ipc_file,
    parse_request_envelope,
)
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.process import OnOutput, is_query_done_pulse
from pynchy.host.container_manager.serialization import parse_container_output
from pynchy.logger import logger
from pynchy.types import GroupFolder

_ipc_watcher_lock = asyncio.Lock()
_ipc_watcher_running = False
_ipc_runtime_sweep_task: asyncio.Task[None] | None = None
IPC_RUNTIME_SWEEP_INTERVAL_SECONDS = 5.0


def _move_to_error_dir(ipc_base_dir: Path, source_group: str, file_path: Path) -> None:
    """Move a failed IPC file to the errors/ directory for later inspection.

    Safe to call inside ``except`` blocks — catches its own OSError so a
    failed move never masks the error being handled or escapes the handler.
    """
    try:
        error_dir = ipc_base_dir / "errors"
        error_dir.mkdir(parents=True, exist_ok=True)
        file_path.rename(error_dir / f"{source_group}-{file_path.name}")
    except OSError:
        logger.warning(
            "Failed to move IPC file to error dir, deleting instead",
            file=file_path.name,
            source_group=source_group,
        )
        with contextlib.suppress(OSError):
            file_path.unlink()


def _log_sweep_error(message: str, exc: OSError, source_group: str) -> None:
    logger.error(message, err=str(exc), source_group=source_group)


def _path_exists(path: Path) -> bool:
    return path.exists()


def _mkdir_parents(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _unlink_path(path: Path) -> None:
    path.unlink()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json_files_in_dir(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(f for f in path.iterdir() if f.suffix == ".json")


def _group_folders_in_ipc_dir(ipc_base_dir: Path) -> list[str]:
    return [f.name for f in ipc_base_dir.iterdir() if f.is_dir() and f.name != "errors"]


async def _process_message_file(
    file_path: Path,
    source_group: str,
    is_admin: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    """Process a single IPC message file."""
    s = get_settings()
    try:
        message = InboundChatMessage.from_dict(parse_ipc_file(file_path))

        if message is not None:
            workspaces = deps.workspaces()
            target_group = workspaces.get(message.chat_jid)
            if is_admin or (target_group and target_group.folder == source_group):
                from pynchy.types import OutboundEvent, OutboundEventType

                prefix = message.sender or s.agent.name
                await deps.broadcast_to_channels(
                    message.chat_jid,
                    OutboundEvent(
                        type=OutboundEventType.TEXT,
                        content=f"{prefix}: {message.text}",
                    ),
                )
                logger.info(
                    "IPC message sent",
                    chat_jid=message.chat_jid,
                    source_group=source_group,
                )
            else:
                logger.warning(
                    "Unauthorized IPC message attempt blocked",
                    chat_jid=message.chat_jid,
                    source_group=source_group,
                )
        await asyncio.to_thread(_unlink_path, file_path)
    except Exception:
        logger.exception(
            "Error processing IPC message",
            file=file_path.name,
            source_group=source_group,
        )
        await asyncio.to_thread(_move_to_error_dir, ipc_base_dir, source_group, file_path)


async def _process_request_file(
    file_path: Path,
    source_group: str,
    is_admin: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    """Process a single canonical IPC request file."""
    try:
        envelope = parse_request_envelope(file_path)
        if envelope.source_group != source_group:
            raise ValueError(
                "IPC request source_group does not match directory "
                f"({envelope.source_group!r} != {source_group!r})"
            )

        if envelope.kind == "refresh_groups":
            await _handle_signal(envelope.kind, source_group, is_admin, deps)
            await asyncio.to_thread(_unlink_path, file_path)
            return

        if _claim_request_for_execution(envelope, ipc_base_dir):
            await dispatch(envelope, source_group, is_admin, deps)
        await asyncio.to_thread(_unlink_path, file_path)
    except Exception:
        logger.exception(
            "Error processing IPC request",
            file=file_path.name,
            source_group=source_group,
        )
        await asyncio.to_thread(_move_to_error_dir, ipc_base_dir, source_group, file_path)


def _get_output_handler(group_folder: str) -> OnOutput | None:
    """Look up the session's output callback for a group.

    Returns None if no session is active or no handler is set.
    Delegates to get_session_output_handler() which is the public API
    on the session module.
    """
    from pynchy.host.container_manager.session import get_session_output_handler

    return get_session_output_handler(GroupFolder(group_folder))


def _signal_query_done(group_folder: str) -> None:
    """Signal query completion for a group's session.

    Delegates to session.signal_query_done() which sets the _query_done
    event, clears the output handler, and resets the idle timer.
    """
    from pynchy.host.container_manager.session import get_session

    session = get_session(GroupFolder(group_folder))
    if session is None:
        return
    session.signal_query_done()


async def _process_output_file(
    file_path: Path,
    source_group: str,
    ipc_base_dir: Path,
) -> None:
    """Process a single output event file from a container.

    Reads JSON, parses via parse_container_output(), dispatches to the
    session's output handler, and detects query-done pulses (result events
    with new_session_id).

    Only deletes the file if a session handler consumed it.  If no handler
    is registered (e.g. a stale output file from a dead session), the file
    is left in place for the startup sweep to clean up.
    """
    with claim_output_file(file_path) as claimed:
        if not claimed:
            return

        await _process_claimed_output_file(file_path, source_group, ipc_base_dir)


async def _process_claimed_output_file(
    file_path: Path,
    source_group: str,
    ipc_base_dir: Path,
) -> None:
    """Process an output file after this task has claimed handler delivery."""
    try:
        try:
            json_str = await asyncio.to_thread(_read_text, file_path)
        except FileNotFoundError:
            # Watchdog and the periodic runtime sweep can both discover the
            # same output file. Whichever loses that race should be a no-op.
            return
        output = parse_container_output(json_str)

        # Dispatch to the session's output handler
        handler = _get_output_handler(source_group)
        if handler is not None:
            try:
                await handler(output)
            except Exception:
                logger.exception(
                    "Output handler callback failed",
                    group=source_group,
                )

        # Detect query-done pulse
        if is_query_done_pulse(output):
            _signal_query_done(source_group)
            logger.info(
                "Query done pulse received via output file",
                group=source_group,
            )

        # Only delete if a session handler consumed the event.  If no
        # handler is set (e.g. stale file from a dead session), leave for
        # the startup sweep to clean up.
        if handler is not None:
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(_unlink_path, file_path)
    except Exception:
        logger.exception(
            "Error processing output file",
            file=file_path.name,
            source_group=source_group,
        )
        await asyncio.to_thread(_move_to_error_dir, ipc_base_dir, source_group, file_path)


async def _sweep_messages(
    messages_dir: Path, source_group: str, is_admin: bool, ipc_base_dir: Path, deps: IpcDeps
) -> int:
    """Replay pending message files. Returns the number processed."""
    try:
        count = 0
        for file_path in await asyncio.to_thread(_json_files_in_dir, messages_dir):
            await _process_message_file(file_path, source_group, is_admin, ipc_base_dir, deps)
            count += 1
    except OSError as exc:
        _log_sweep_error("Error reading IPC messages directory during sweep", exc, source_group)
        return 0
    else:
        return count


async def _sweep_requests(
    requests_dir: Path, source_group: str, is_admin: bool, ipc_base_dir: Path, deps: IpcDeps
) -> int:
    """Replay pending request files. Returns the number processed."""
    try:
        count = 0
        for file_path in await asyncio.to_thread(_json_files_in_dir, requests_dir):
            await _process_request_file(file_path, source_group, is_admin, ipc_base_dir, deps)
            count += 1
    except OSError as exc:
        _log_sweep_error("Error reading IPC requests directory during sweep", exc, source_group)
        return 0
    else:
        return count


async def _sweep_output_events(output_dir: Path, source_group: str, ipc_base_dir: Path) -> int:
    """Process output files during live runtime recovery sweeps."""
    try:
        count = 0
        for file_path in await asyncio.to_thread(_json_files_in_dir, output_dir):
            await _process_output_file(file_path, source_group, ipc_base_dir)
            count += 1
    except OSError as exc:
        _log_sweep_error(
            "Error reading IPC output directory during runtime sweep", exc, source_group
        )
        return 0
    else:
        return count


async def _sweep_approval_decisions(decisions_dir: Path, source_group: str, deps: IpcDeps) -> int:
    """Process approval decisions during live runtime recovery sweeps."""
    try:
        count = 0
        from pynchy.host.container_manager.ipc.handlers_approval import (
            process_approval_decision,
        )

        for file_path in await asyncio.to_thread(_json_files_in_dir, decisions_dir):
            await process_approval_decision(file_path, source_group, deps=deps)
            count += 1
    except OSError as exc:
        _log_sweep_error(
            "Error reading IPC approval_decisions directory during runtime sweep",
            exc,
            source_group,
        )
        return 0
    else:
        return count


def _clean_output_dir(output_dir: Path, source_group: str) -> int:
    """Delete stale output files — mid-query artefacts from a dead session;
    replaying them on crash recovery is meaningless since there's no active
    session to dispatch to. Returns the number deleted.
    """
    if not output_dir.exists():
        return 0
    try:
        count = 0
        for file_path in sorted(f for f in output_dir.iterdir() if f.suffix == ".json"):
            file_path.unlink()
            count += 1
    except OSError as exc:
        _log_sweep_error("Error cleaning IPC output directory during sweep", exc, source_group)
        return 0
    else:
        return count


def _clean_stale_initial(input_dir: Path, source_group: str) -> int:
    """Delete a stale initial.json — a cold-start prompt never consumed
    because the container crashed before reading it. Returns 1 if deleted.
    """
    try:
        initial_file = input_dir / "initial.json"
        if initial_file.exists():
            initial_file.unlink()
            return 1
    except OSError as exc:
        _log_sweep_error("Error cleaning stale initial.json during sweep", exc, source_group)
        return 0
    else:
        return 0


async def _sweep_expired_state() -> None:
    """Auto-deny/expire stale approvals and pending questions left from a crash."""
    from pynchy.host.container_manager.security.approval import sweep_expired_approvals

    expired = await sweep_expired_approvals()
    if expired:
        logger.info("Expired approvals auto-denied during sweep", count=len(expired))

    from pynchy.host.orchestrator.messaging.pending_questions import sweep_expired_questions

    expired_qs = await sweep_expired_questions()
    if expired_qs:
        logger.info("Expired pending questions auto-expired during sweep", count=len(expired_qs))


async def _sweep_directory(
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> int:
    """Sweep stale IPC files on startup (crash recovery).

    Messages and requests are *processed* (replayed).  Output files and stale
    ``initial.json`` are *deleted* — they were mid-query artefacts from a
    dead session and replaying them is meaningless.

    Returns the total number of files handled (processed + cleaned).
    """
    try:
        group_folders = await asyncio.to_thread(_group_folders_in_ipc_dir, ipc_base_dir)
    except OSError as exc:
        logger.error("Error reading IPC base directory during sweep", err=str(exc))
        return 0

    workspaces = deps.workspaces()
    admin_folders = {g.folder for g in workspaces.values() if g.is_admin}

    processed = 0
    cleaned = 0
    for source_group in group_folders:
        is_admin = source_group in admin_folders
        group_dir = ipc_base_dir / source_group
        processed += await _sweep_messages(
            group_dir / "messages", source_group, is_admin, ipc_base_dir, deps
        )
        processed += await _sweep_requests(
            group_dir / "requests", source_group, is_admin, ipc_base_dir, deps
        )
        cleaned += await asyncio.to_thread(_clean_output_dir, group_dir / "output", source_group)
        cleaned += await asyncio.to_thread(_clean_stale_initial, group_dir / "input", source_group)

    if cleaned > 0:
        logger.info("IPC startup sweep cleaned stale files", cleaned=cleaned)

    await _sweep_expired_state()

    return processed + cleaned


async def _sweep_runtime_directory(
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> int:
    """Sweep live IPC directories so missed watchdog events do not wedge sessions."""
    try:
        group_folders = await asyncio.to_thread(_group_folders_in_ipc_dir, ipc_base_dir)
    except OSError as exc:
        logger.error("Error reading IPC base directory during runtime sweep", err=str(exc))
        return 0

    workspaces = deps.workspaces()
    admin_folders = {g.folder for g in workspaces.values() if g.is_admin}

    processed = 0
    for source_group in group_folders:
        is_admin = source_group in admin_folders
        group_dir = ipc_base_dir / source_group
        processed += await _sweep_messages(
            group_dir / "messages", source_group, is_admin, ipc_base_dir, deps
        )
        processed += await _sweep_requests(
            group_dir / "requests", source_group, is_admin, ipc_base_dir, deps
        )
        processed += await _sweep_output_events(group_dir / "output", source_group, ipc_base_dir)
        processed += await _sweep_approval_decisions(
            group_dir / "approval_decisions", source_group, deps
        )

    await _sweep_expired_state()

    return processed


async def _runtime_sweep_loop(ipc_base_dir: Path, deps: IpcDeps) -> None:
    """Periodically sweep live IPC files in case watchdog drops an event."""
    while True:
        await asyncio.sleep(IPC_RUNTIME_SWEEP_INTERVAL_SECONDS)
        handled = await _sweep_runtime_directory(ipc_base_dir, deps)
        if handled:
            logger.info("IPC runtime sweep processed files", count=handled)


async def _process_queue(
    queue: asyncio.Queue[Path],
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    """Consume the event queue and dispatch IPC files."""
    while True:
        file_path = await queue.get()
        try:
            if not await asyncio.to_thread(_path_exists, file_path):
                continue

            relative = file_path.relative_to(ipc_base_dir)
            parts = relative.parts
            source_group = parts[0]
            subdir = parts[1]

            # Re-check admin status (groups can change at runtime)
            current_groups = deps.workspaces()
            current_admin_folders = {g.folder for g in current_groups.values() if g.is_admin}
            is_admin = source_group in current_admin_folders

            if subdir == "messages":
                await _process_message_file(file_path, source_group, is_admin, ipc_base_dir, deps)
            elif subdir == "requests":
                await _process_request_file(file_path, source_group, is_admin, ipc_base_dir, deps)
            elif subdir == "output":
                await _process_output_file(file_path, source_group, ipc_base_dir)
            elif subdir == "approval_decisions":
                from pynchy.host.container_manager.ipc.handlers_approval import (
                    process_approval_decision,
                )

                await process_approval_decision(file_path, source_group, deps=deps)
        except Exception:
            logger.exception(
                "Error processing queued IPC file",
                file=str(file_path),
            )
        finally:
            queue.task_done()


async def start_ipc_watcher(deps: IpcDeps) -> None:
    """Start the IPC watcher using watchdog filesystem events.

    1. Performs a startup sweep to process files written while the process was down.
    2. Starts a watchdog Observer for event-driven processing.
    """
    global _ipc_runtime_sweep_task, _ipc_watcher_running
    async with _ipc_watcher_lock:
        if _ipc_watcher_running:
            logger.debug("IPC watcher already running, skipping duplicate start")
            return
        _ipc_watcher_running = True

    s = get_settings()
    ipc_base_dir = s.data_dir / "ipc"
    await asyncio.to_thread(_mkdir_parents, ipc_base_dir)

    # --- Startup sweep (crash recovery) ---
    swept = await _sweep_directory(ipc_base_dir, deps)
    if swept > 0:
        logger.info("IPC startup sweep processed files", count=swept)

    # --- Start watchdog observer ---
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()

    handler = _IpcEventHandler(ipc_base_dir, loop, queue)
    observer = Observer()
    observer.schedule(handler, str(ipc_base_dir), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info("IPC watcher started (watchdog mode)", path=str(ipc_base_dir))

    _ipc_runtime_sweep_task = asyncio.create_task(_runtime_sweep_loop(ipc_base_dir, deps))

    await _process_queue(queue, ipc_base_dir, deps)
