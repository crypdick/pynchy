"""Names for host runtime resources shared by one Pynchy instance."""

from __future__ import annotations

import os
import re

_DEFAULT_NAMESPACE = "pynchy"
_NAMESPACE_ENV = "PYNCHY_RUNTIME_NAMESPACE"
_VALID_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")


def runtime_namespace() -> str:
    """Return the validated Docker resource namespace for this process."""
    namespace = os.environ.get(_NAMESPACE_ENV, _DEFAULT_NAMESPACE).strip()
    if not _VALID_NAMESPACE.fullmatch(namespace):
        message = (
            f"{_NAMESPACE_ENV} must start with a lowercase letter or digit and contain "
            "only lowercase letters, digits, dots, underscores, or dashes"
        )
        raise ValueError(message)
    return namespace


def runtime_container_name(suffix: str) -> str:
    """Return a container name scoped to the current Pynchy instance."""
    return f"{runtime_namespace()}-{suffix}"


def runtime_network_name(suffix: str) -> str:
    """Return a Docker network name scoped to the current Pynchy instance."""
    return f"{runtime_namespace()}-{suffix}"


def runtime_volume_name(suffix: str) -> str:
    """Return a Docker volume name scoped to the current Pynchy instance."""
    return f"{runtime_namespace()}-{suffix}"
