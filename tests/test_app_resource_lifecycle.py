"""Public resource-lifecycle behavior for the application composition root."""

from __future__ import annotations

from pynchy.host.orchestrator.app import PynchyApp


class _CloseableObserver:
    name = "test-observer"

    def __init__(self) -> None:
        self.closed = False

    def subscribe(self, _event_bus: object) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _MemoryProvider:
    name = "test-memory"

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False

    async def init(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def save(
        self,
        _group_folder: object,
        _key: str,
        _content: str,
        category: str = "core",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {"category": category, "metadata": metadata}

    async def recall(
        self,
        _group_folder: object,
        _query: str,
        category: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        return [{"category": category, "limit": limit}]

    async def forget(self, _group_folder: object, _key: str) -> dict[str, object]:
        return {}

    async def list_keys(
        self, _group_folder: object, category: str | None = None
    ) -> list[dict[str, object]]:
        return [{"category": category}]


class _HttpRunner:
    def __init__(self) -> None:
        self.cleaned = False

    async def cleanup(self) -> None:
        self.cleaned = True


async def test_application_owns_attached_resources_through_shutdown() -> None:
    app = PynchyApp()
    observer = _CloseableObserver()
    memory = _MemoryProvider()
    runner = _HttpRunner()

    app.attach_observers([observer])
    await app.set_memory_provider(memory)
    app.set_http_runner(runner)

    assert memory.initialized is True
    await app.close_observers()
    await app.close_memory_provider()
    await app.cleanup_http_runner()
    await app.cleanup_http_runner()

    assert observer.closed is True
    assert memory.closed is True
    assert runner.cleaned is True


def test_application_shutdown_transition_is_idempotent() -> None:
    app = PynchyApp()

    assert app.is_shutting_down() is False
    assert app.begin_shutdown() is True
    assert app.is_shutting_down() is True
    assert app.begin_shutdown() is False


def test_application_dispatch_cursor_preserves_the_furthest_in_flight_message() -> None:
    app = PynchyApp()
    app.last_agent_timestamp["chat"] = "2026-07-28T10:00:00Z"

    app.mark_dispatched("chat", "2026-07-28T10:00:01Z")
    app.mark_dispatched("chat", "2026-07-28T10:00:00Z")

    assert app.routing_cursor("chat") == "2026-07-28T10:00:01Z"
    assert app.pop_dispatched("chat", "fallback") == "2026-07-28T10:00:01Z"
    assert app.routing_cursor("chat") == "2026-07-28T10:00:00Z"
