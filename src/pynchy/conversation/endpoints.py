from __future__ import annotations

import os
from collections.abc import (
    Mapping,  # noqa: TC003, RUF100 - beartype resolves annotations at runtime.
)


def _normalized_endpoint(value: str | None) -> str | None:
    endpoint = value.strip().rstrip("/") if value is not None else ""
    if endpoint.endswith("/v1/traces"):
        endpoint = endpoint.removesuffix("/v1/traces").rstrip("/")
    return endpoint or None


def resolved_phoenix_endpoint(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    base = _normalized_endpoint(source.get("PHOENIX_COLLECTOR_ENDPOINT"))
    if base:
        return base
    return _normalized_endpoint(source.get("PHOENIX_COLLECTOR_HTTP_ENDPOINT"))
