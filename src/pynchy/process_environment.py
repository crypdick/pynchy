"""Safe host process environment selection."""

from __future__ import annotations

import os
from collections.abc import (  # noqa: TC003 - beartype resolves this runtime annotation.
    Mapping,
)

_SAFE_HOST_ENVIRONMENT = ("HOME", "PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL")


def filtered_process_environment(
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the safe host baseline plus explicitly selected values."""
    environment = {name: os.environ[name] for name in _SAFE_HOST_ENVIRONMENT if name in os.environ}
    if extra:
        environment.update(extra)
    return environment
