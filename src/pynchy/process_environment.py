"""Safe host process environment selection."""

from __future__ import annotations

import os
from collections.abc import (
    Mapping,
)

_SAFE_HOST_ENVIRONMENT = (
    "HOME",
    "PATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "PYNCHY_KUBECONFIG",
    "PYNCHY_KUBERNETES_NAMESPACE",
    "PYNCHY_KUBERNETES_PVC",
    "PYNCHY_KUBERNETES_VAULT_PVC",
    "PYNCHY_KUBERNETES_SHARED_ROOT",
    "PYNCHY_KUBERNETES_PULL_POLICY",
)


def filtered_process_environment(
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the safe host baseline plus explicitly selected values."""
    environment = {name: os.environ[name] for name in _SAFE_HOST_ENVIRONMENT if name in os.environ}
    if extra:
        environment.update(extra)
    return environment
