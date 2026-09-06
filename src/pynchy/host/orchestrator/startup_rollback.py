"""Durability and process termination for failed deploy startup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn


def ensure_rollback_evidence_durable(
    continuation_path: Path,
    boot_warning_path: Path,
) -> None:
    """Persist rewritten rollback evidence before an immediate process exit."""
    for path in (continuation_path, boot_warning_path):
        with path.open("rb") as evidence:
            os.fsync(evidence.fileno())

    # File fsyncs persist contents; the directory fsync persists both atomic
    # renames. Closing descriptors before os._exit does not provide either
    # durability guarantee.
    parent_descriptor = os.open(continuation_path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def terminate_failed_startup() -> NoReturn:
    """Exit immediately so a service manager can restart a rolled-back host."""
    # Plugin-created non-daemon threads can keep Python alive after SystemExit,
    # leaving the rolled-back process wedged without its control socket.
    os._exit(1)
