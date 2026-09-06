"""Publication gate for the HTTP control plane."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from aiohttp import web


@dataclass(slots=True)
class ControlPlaneReadiness:
    """Reject requests until every runtime owner is ready."""

    accepting_requests: bool = False


readiness_key: web.AppKey[ControlPlaneReadiness] = web.AppKey("readiness")


@web.middleware
async def readiness_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    readiness = request.app[readiness_key]
    if not readiness.accepting_requests:
        return web.json_response({"status": "starting"}, status=503)
    return await handler(request)
