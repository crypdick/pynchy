"""File-based IPC watcher.

Uses watchdog for event-driven processing and startup recovery.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path  # beartype resolves IPC watcher paths at runtime.

from watchdog.observers import Observer

from pynchy.host.container_manager.ipc.approval_recovery import sweep_host_approval_decisions
from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # beartype resolves IPC watcher deps at runtime.
)
from pynchy.host.container_manager.ipc.events import IpcEventHandler
from pynchy.host.container_manager.ipc.file_claims import (
    claim_ipc_file,
    move_failed_ipc_file,
    release_ipc_file,
)
from pynchy.host.container_manager.ipc.handlers_signals import handle_signal
from pynchy.host.container_manager.ipc.input_processing import (
    classify_queued_ipc_file,
    handle_message_file,
)
from pynchy.host.container_manager.ipc.ledger import (
    claim_request_for_execution as _claim_request_for_execution,
)
from pynchy.host.container_manager.ipc.output_processing import process_output_file
from pynchy.host.container_manager.ipc.protocol import parse_request_envelope
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.logger import logger

_ipc_watcher_lock = asyncio.Lock()
IPC_RUNTIME_SWEEP_INTERVAL_SECONDS = 5.0
_IPC_SOURCE_GROUP_MISMATCH_ERROR = (
    "IPC request source_group does not match directory ({actual!r} != {expected!r})"
)


@dataclass
class _WatcherState:
    running: bool = False
    runtime_sweep_task: asyncio.Task[None] | None = None


_state = _WatcherState()


def _log_sweep_error(message: str, exc: OSError, source_group: str) -> None:
    logger.error(message, err=str(exc), source_group=source_group)


def _json_files_in_dir(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(f for f in path.iterdir() if f.suffix == ".json")


def _group_folders_in_ipc_dir(ipc_base_dir: Path) -> list[str]:
    return [f.name for f in ipc_base_dir.iterdir() if f.is_dir() and f.name != "errors"]


async def process_ipc_message_file(
    file_path: Path,
    source_group: str,
    *,
    is_admin: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    """Process a single IPC message file."""
    if not claim_ipc_file(file_path):
        return
    try:
        await handle_message_file(file_path, source_group, is_admin=is_admin, deps=deps)
    except Exception:  # noqa: BLE001 - IPC message handling is an isolation boundary; move failures to error dir.
        logger.exception(
            "Error processing IPC message",
            file=file_path.name,
            source_group=source_group,
        )
        await asyncio.to_thread(move_failed_ipc_file, ipc_base_dir, source_group, file_path)
    finally:
        release_ipc_file(file_path)


async def process_ipc_request_file(
    file_path: Path,
    source_group: str,
    *,
    is_admin: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    """Process a single canonical IPC request file."""
    if not claim_ipc_file(file_path):
        return
    try:
        await _handle_request_file(
            file_path,
            source_group,
            is_admin=is_admin,
            ipc_base_dir=ipc_base_dir,
            deps=deps,
        )
    except Exception:  # noqa: BLE001 - IPC request handling is an isolation boundary; move failures to error dir.
        logger.exception(
            "Error processing IPC request",
            file=file_path.name,
            source_group=source_group,
        )
        await asyncio.to_thread(move_failed_ipc_file, ipc_base_dir, source_group, file_path)
    finally:
        release_ipc_file(file_path)


async def _handle_request_file(
    file_path: Path,
    source_group: str,
    *,
    is_admin: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    envelope = parse_request_envelope(file_path)
    if envelope.source_group != source_group:
        raise ValueError(
            _IPC_SOURCE_GROUP_MISMATCH_ERROR.format(
                actual=envelope.source_group,
                expected=source_group,
            )
        )

    if envelope.kind == "refresh_groups":
        await handle_signal(
            envelope.kind,
            source_group,
            is_admin=is_admin,
            deps=deps,
        )
        await asyncio.to_thread(file_path.unlink)
        return

    if _claim_request_for_execution(envelope, ipc_base_dir):
        await dispatch(envelope, source_group, is_admin=is_admin, deps=deps)
    await asyncio.to_thread(file_path.unlink)


async def _sweep_messages(
    messages_dir: Path,
    source_group: str,
    *,
    is_admin: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> int:
    """Replay pending message files. Returns the number processed."""
    try:
        count = 0
        for file_path in await asyncio.to_thread(_json_files_in_dir, messages_dir):
            await process_ipc_message_file(
                file_path,
                source_group,
                is_admin=is_admin,
                ipc_base_dir=ipc_base_dir,
                deps=deps,
            )
            count += 1
    except OSError as exc:
        _log_sweep_error("Error reading IPC messages directory during sweep", exc, source_group)
        return 0
    else:
        return count


async def _sweep_requests(
    requests_dir: Path,
    source_group: str,
    *,
    is_admin: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> int:
    """Replay pending request files. Returns the number processed."""
    try:
        count = 0
        for file_path in await asyncio.to_thread(_json_files_in_dir, requests_dir):
            await process_ipc_request_file(
                file_path,
                source_group,
                is_admin=is_admin,
                ipc_base_dir=ipc_base_dir,
                deps=deps,
            )
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
            await process_output_file(file_path, source_group, ipc_base_dir)
            count += 1
    except OSError as exc:
        _log_sweep_error(
            "Error reading IPC output directory during runtime sweep", exc, source_group
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


async def _sweep_expired_state(deps: IpcDeps) -> None:
    """Auto-deny/expire stale approvals and pending questions left from a crash."""
    from pynchy.host.container_manager.security.approval import (  # noqa: PLC0415 - approval state pulls IPC dispatch helpers; load only when sweeping.
        sweep_expired_approvals,
    )

    expired = await sweep_expired_approvals(deps.expire_action_intent)
    if expired:
        logger.info("Expired approvals auto-denied during sweep", count=len(expired))

    def write_expiration_response(group_name: str, request_id: str, error: str) -> None:
        write_ipc_response(ipc_response_path(group_name, request_id), {"error": error})

    expired_qs = await deps.sweep_expired_questions(write_expiration_response)
    if expired_qs:
        logger.info("Expired pending questions auto-expired during sweep", count=len(expired_qs))


async def recover_ipc_startup(
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
            group_dir / "messages",
            source_group,
            is_admin=is_admin,
            ipc_base_dir=ipc_base_dir,
            deps=deps,
        )
        processed += await _sweep_requests(
            group_dir / "requests",
            source_group,
            is_admin=is_admin,
            ipc_base_dir=ipc_base_dir,
            deps=deps,
        )
        cleaned += await asyncio.to_thread(_clean_output_dir, group_dir / "output", source_group)
        cleaned += await asyncio.to_thread(_clean_stale_initial, group_dir / "input", source_group)

    if cleaned > 0:
        logger.info("IPC startup sweep cleaned stale files", cleaned=cleaned)

    await _sweep_expired_state(deps)

    return processed + cleaned


async def recover_ipc_runtime(
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
            group_dir / "messages",
            source_group,
            is_admin=is_admin,
            ipc_base_dir=ipc_base_dir,
            deps=deps,
        )
        processed += await _sweep_requests(
            group_dir / "requests",
            source_group,
            is_admin=is_admin,
            ipc_base_dir=ipc_base_dir,
            deps=deps,
        )
        processed += await _sweep_output_events(group_dir / "output", source_group, ipc_base_dir)

    # Approval state is host-owned and intentionally outside the watched IPC
    # mount. The command path processes decisions immediately; this sweep only
    # recovers a file persisted immediately before a host crash.
    processed += await sweep_host_approval_decisions(deps)

    await _sweep_expired_state(deps)

    return processed


async def _runtime_sweep_loop(ipc_base_dir: Path, deps: IpcDeps) -> None:
    """Periodically sweep live IPC files in case watchdog drops an event."""
    while True:
        await asyncio.sleep(IPC_RUNTIME_SWEEP_INTERVAL_SECONDS)
        handled = await recover_ipc_runtime(ipc_base_dir, deps)
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
            await _dispatch_queued_ipc_file(file_path, ipc_base_dir, deps)
        except Exception:  # noqa: BLE001 - queued IPC file errors stay scoped to one file.
            logger.exception(
                "Error processing queued IPC file",
                file=str(file_path),
            )
        finally:
            queue.task_done()


async def _dispatch_queued_ipc_file(file_path: Path, ipc_base_dir: Path, deps: IpcDeps) -> None:
    queued = await classify_queued_ipc_file(file_path, ipc_base_dir, deps)
    if queued is None:
        return

    if queued.subdir == "messages":
        await process_ipc_message_file(
            queued.path,
            queued.source_group,
            is_admin=queued.is_admin,
            ipc_base_dir=ipc_base_dir,
            deps=deps,
        )
    elif queued.subdir == "requests":
        await process_ipc_request_file(
            queued.path,
            queued.source_group,
            is_admin=queued.is_admin,
            ipc_base_dir=ipc_base_dir,
            deps=deps,
        )
    elif queued.subdir == "output":
        await process_output_file(queued.path, queued.source_group, ipc_base_dir)


async def start_ipc_watcher(deps: IpcDeps, *, ipc_base_dir: Path) -> None:
    """Start the IPC watcher using watchdog filesystem events.

    1. Performs a startup sweep to process files written while the process was down.
    2. Starts a watchdog Observer for event-driven processing.
    """
    async with _ipc_watcher_lock:
        if _state.running:
            logger.debug("IPC watcher already running, skipping duplicate start")
            return
        _state.running = True

    await asyncio.to_thread(ipc_base_dir.mkdir, parents=True, exist_ok=True)

    # --- Startup sweep (crash recovery) ---
    swept = await recover_ipc_startup(ipc_base_dir, deps)
    if swept > 0:
        logger.info("IPC startup sweep processed files", count=swept)

    # --- Start watchdog observer ---
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()

    handler = IpcEventHandler(ipc_base_dir, loop, queue)
    observer = Observer()
    observer.schedule(handler, str(ipc_base_dir), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info("IPC watcher started (watchdog mode)", path=str(ipc_base_dir))

    _state.runtime_sweep_task = asyncio.create_task(_runtime_sweep_loop(ipc_base_dir, deps))

    await _process_queue(queue, ipc_base_dir, deps)
