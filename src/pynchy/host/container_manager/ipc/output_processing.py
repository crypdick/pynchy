"""Process file-based IPC output events."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from pynchy.agent_protocol.api import (
    OnOutput,
    parse_container_output,
)
from pynchy.host.container_manager import session as session_manager
from pynchy.host.container_manager.ipc.file_claims import move_failed_ipc_file
from pynchy.host.container_manager.process import is_query_done_pulse
from pynchy.identifiers import GroupFolder
from pynchy.logger import logger

# Watchdog and runtime recovery can observe different files concurrently.
# Serialize each group so a later file cannot overtake an in-flight callback.
_output_group_locks: dict[str, asyncio.Lock] = {}


def _get_output_handler(group_folder: str) -> OnOutput | None:
    """Look up the session's output callback for a group."""
    return session_manager.get_session_output_handler(GroupFolder(group_folder))


def _signal_query_progress(group_folder: str, query_id: str | None) -> bool | None:
    """Refresh the active query, or report output from another generation."""
    session = session_manager.get_session(GroupFolder(group_folder))
    if session is None or session.output_handler is None:
        return None
    return session.signal_query_progress(query_id) is not False


def _signal_query_done(group_folder: str, query_id: str | None) -> bool:
    """Signal query completion for a group's matching active generation."""
    session = session_manager.get_session(GroupFolder(group_folder))
    if session is None:
        return False
    return session.signal_query_done(query_id) is not False


async def process_output_file(
    file_path: Path,
    source_group: str,
    ipc_base_dir: Path,
) -> None:
    """Process a single output event file from a container."""
    async with _output_group_locks.setdefault(source_group, asyncio.Lock()):
        try:
            await _handle_claimed_output_file(file_path, source_group)
        except Exception:  # noqa: BLE001 - output file processing is an isolation boundary.
            logger.exception(
                "Error processing output file",
                file=file_path.name,
                source_group=source_group,
            )
            await asyncio.to_thread(move_failed_ipc_file, ipc_base_dir, source_group, file_path)


async def _handle_claimed_output_file(file_path: Path, source_group: str) -> None:
    try:
        json_str = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
    except FileNotFoundError:
        # Watchdog and the periodic runtime sweep can both discover the
        # same output file. Whichever loses that race should be a no-op.
        return

    output = parse_container_output(json_str)

    progress_accepted = _signal_query_progress(source_group, output.query_id)
    if progress_accepted is False:
        logger.warning(
            "Discarding stale output from a prior query generation",
            group=source_group,
            query_id=output.query_id,
        )
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(file_path.unlink)
        return

    handler = _get_output_handler(source_group)
    if handler is not None:
        try:
            await handler(output)
        except Exception:  # noqa: BLE001 - output handler is a delivery boundary; keep the file alive.
            logger.exception(
                "Output handler callback failed",
                group=source_group,
            )
            return

    if is_query_done_pulse(output) and _signal_query_done(source_group, output.query_id):
        logger.info(
            "Query done pulse received via output file",
            group=source_group,
            query_id=output.query_id,
        )

    if handler is not None:
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(file_path.unlink)
