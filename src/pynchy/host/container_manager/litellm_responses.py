"""Bounded, sanitized LiteLLM Responses availability checks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

from pynchy.host.container_manager.litellm_config import (  # noqa: TC001 - beartype resolves Responses route types at runtime.
    ResponseModelRoute,
)
from pynchy.logger import logger

_RESPONSE_CANARY_TIMEOUT = 10.0
_RESPONSE_REFRESH_INTERVAL = 60.0


@dataclass(frozen=True)
class _ResponseAliasAvailability:
    """Sanitized availability result for one LiteLLM Responses alias."""

    alias: str
    route_count: int
    state: str
    checked_at: str
    failure: str | None = None


def _response_canary_payload(model: str) -> dict[str, object]:
    """Build the minimal provider-safe Responses stream request."""
    # Responses requires nonempty content; this fixed inert token never carries user data.
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "."}],
            }
        ],
        "stream": True,
        "max_output_tokens": 1,
    }


def _http_failure_class(status: int) -> str:
    """Return an allowlisted status category without retaining response content."""
    if 100 <= status < 600:
        return f"http_{status // 100}xx"
    return "http_other"


class LiteLLMResponsesAvailability:
    """Cache sanitized Responses availability and refresh it outside status requests."""

    def __init__(self, *, port: int, key: str) -> None:
        self._port = port
        self._key = key
        self._routes: tuple[ResponseModelRoute, ...] = ()
        self._availability: tuple[_ResponseAliasAvailability, ...] = ()
        self._checked_at: str | None = None
        self._refreshed_at: float | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    def set_routes(self, routes: tuple[ResponseModelRoute, ...]) -> None:
        """Set active routes from the generated LiteLLM config."""
        self._routes = routes
        self._availability = ()
        self._checked_at = None
        self._refreshed_at = None

    @property
    def state(self) -> str:
        if not self._routes:
            return "not_configured"
        if self._availability and all(item.state == "available" for item in self._availability):
            return "available"
        return "unavailable"

    @property
    def status(self) -> dict[str, object]:
        """Return cached availability and schedule one stale refresh without blocking."""
        stale = self._is_stale()
        if stale:
            self._schedule_refresh()
        return {
            "state": self.state,
            "checked_at": self._checked_at,
            "stale": stale,
            "aliases": [
                {
                    "alias": item.alias,
                    "route_count": item.route_count,
                    "state": item.state,
                    "checked_at": item.checked_at,
                    "failure": item.failure,
                }
                for item in self._availability
            ],
        }

    async def refresh(self) -> None:
        """Run one sequential canary per configured Responses alias."""
        checked_at = datetime.now(UTC).isoformat()
        if not self._routes:
            self._availability = ()
            self._checked_at = None
            self._refreshed_at = time.monotonic()
            return

        self._availability = await self._probe_all(checked_at)
        self._checked_at = checked_at
        self._refreshed_at = time.monotonic()
        if self.state == "unavailable":
            logger.warning(
                "LiteLLM Responses availability unavailable",
                aliases=[item.alias for item in self._availability if item.state == "unavailable"],
                failures=sorted({item.failure for item in self._availability if item.failure}),
            )

    async def stop(self) -> None:
        """Cancel a pending stale refresh before its gateway disappears."""
        task = self._refresh_task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._refresh_task = None

    def _is_stale(self) -> bool:
        if not self._routes:
            return False
        if self._refreshed_at is None:
            return True
        return (time.monotonic() - self._refreshed_at) >= _RESPONSE_REFRESH_INTERVAL

    def _schedule_refresh(self) -> None:
        if self._refresh_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._refresh_task = loop.create_task(self._refresh_in_background())

    async def _refresh_in_background(self) -> None:
        try:
            await self.refresh()
        finally:
            self._refresh_task = None

    async def _probe_all(self, checked_at: str) -> tuple[_ResponseAliasAvailability, ...]:
        timeout = aiohttp.ClientTimeout(total=_RESPONSE_CANARY_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                return tuple(
                    [await self._probe_alias(session, route, checked_at) for route in self._routes]
                )
        except TimeoutError:
            failure = "timeout"
        except aiohttp.ClientError:
            failure = "network"
        except Exception:  # noqa: BLE001  # allow: exception-handling - canary failures must remain sanitized.
            failure = "protocol"
        return self._unavailable_routes(checked_at, failure)

    async def _probe_alias(
        self,
        session: aiohttp.ClientSession,
        route: ResponseModelRoute,
        checked_at: str,
    ) -> _ResponseAliasAvailability:
        if route.canary_model is None:
            return self._unavailable_route(route, checked_at, "not_probeable")
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        try:
            async with session.post(
                f"http://localhost:{self._port}/v1/responses",
                headers=headers,
                json=_response_canary_payload(route.canary_model),
            ) as response:
                if response.status != 200:
                    return self._unavailable_route(
                        route,
                        checked_at,
                        _http_failure_class(response.status),
                    )
                terminal_done = False
                async for line in response.content:
                    if line.strip():
                        terminal_done = line.strip() == b"data: [DONE]"
        except TimeoutError:
            return self._unavailable_route(route, checked_at, "timeout")
        except aiohttp.ClientError:
            return self._unavailable_route(route, checked_at, "network")
        except Exception:  # noqa: BLE001  # allow: exception-handling - canary failures must remain sanitized.
            return self._unavailable_route(route, checked_at, "protocol")

        return (
            _ResponseAliasAvailability(
                alias=route.model,
                route_count=route.route_count,
                state="available",
                checked_at=checked_at,
            )
            if terminal_done
            else self._unavailable_route(route, checked_at, "protocol")
        )

    @staticmethod
    def _unavailable_route(
        route: ResponseModelRoute,
        checked_at: str,
        failure: str,
    ) -> _ResponseAliasAvailability:
        return _ResponseAliasAvailability(
            alias=route.model,
            route_count=route.route_count,
            state="unavailable",
            checked_at=checked_at,
            failure=failure,
        )

    def _unavailable_routes(
        self,
        checked_at: str,
        failure: str,
    ) -> tuple[_ResponseAliasAvailability, ...]:
        return tuple(self._unavailable_route(route, checked_at, failure) for route in self._routes)
