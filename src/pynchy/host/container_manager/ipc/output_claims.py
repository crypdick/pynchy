"""In-process claims for IPC output file delivery."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_output_files_in_progress: set[Path] = set()


@contextlib.contextmanager
def claim_output_file(file_path: Path) -> Iterator[bool]:
    """Return False when another watcher task is already consuming this output file."""
    claim_path = file_path.absolute()
    if claim_path in _output_files_in_progress:
        yield False
        return

    _output_files_in_progress.add(claim_path)
    try:
        yield True
    finally:
        _output_files_in_progress.discard(claim_path)
