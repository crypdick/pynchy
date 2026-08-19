"""Names for host runtime resources shared by one Pynchy instance."""

from __future__ import annotations

import hashlib
import os
import re

PRODUCTION_NAMESPACE = "pynchy"
_DEFAULT_NAMESPACE = PRODUCTION_NAMESPACE
_NAMESPACE_ENV = "PYNCHY_RUNTIME_NAMESPACE"
_VALID_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
_MAX_CONTAINER_NAME_LENGTH = 64
_CONTAINER_NAME_HASH_LENGTH = 12


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
    """Return a runtime-scoped container name accepted by Apple Container.

    Dynamic Discord-thread workspaces include a channel snowflake in their
    folder name. Keep their resulting container names within Apple's 64-byte
    container-ID limit while retaining a suffix-derived collision guard.
    """
    namespace = runtime_namespace()
    name = f"{namespace}-{suffix}"
    if len(name) <= _MAX_CONTAINER_NAME_LENGTH:
        return name

    digest = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:_CONTAINER_NAME_HASH_LENGTH]
    prefix_length = _MAX_CONTAINER_NAME_LENGTH - len(namespace) - len(digest) - 2
    if prefix_length < 1:
        message = f"{_NAMESPACE_ENV} leaves no room for a valid container name"
        raise ValueError(message)
    return f"{namespace}-{suffix[:prefix_length]}-{digest}"


def runtime_network_name(suffix: str) -> str:
    """Return a Docker network name scoped to the current Pynchy instance."""
    return f"{runtime_namespace()}-{suffix}"


def runtime_volume_name(suffix: str) -> str:
    """Return a Docker volume name scoped to the current Pynchy instance."""
    return f"{runtime_namespace()}-{suffix}"
