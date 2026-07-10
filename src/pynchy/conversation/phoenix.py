from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType  # noqa: TC003 - beartype resolves protocol annotations.
from typing import Protocol, Self, cast, runtime_checkable

from phoenix.otel import register

from pynchy.conversation.events import (  # noqa: TC001 - beartype resolves annotations.
    ConversationEvent,
)


class PhoenixWriteError(RuntimeError):
    """Raised when Phoenix rejects or fails to record durable conversation content."""


@dataclass(frozen=True, slots=True)
class PhoenixEventRef:
    event_id: str
    trace_ref: str


class ConversationBodyStore(Protocol):
    async def write_event(self, event: ConversationEvent) -> PhoenixEventRef: ...


@runtime_checkable
class _StartedSpan(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None: ...

    def add_event(self, name: str, attributes: dict[str, object]) -> None: ...


@runtime_checkable
class _Tracer(Protocol):
    def start_as_current_span(
        self, name: str, *, attributes: dict[str, object]
    ) -> _StartedSpan: ...


def phoenix_tracer(project_name: str, endpoint: str | None = None) -> _Tracer:
    if endpoint:
        provider = register(
            project_name=project_name,
            endpoint=endpoint,
            auto_instrument=False,
            batch=False,
            protocol="http/protobuf",
            verbose=False,
            set_global_tracer_provider=False,
        )
        return cast("_Tracer", provider.get_tracer("pynchy.conversation"))
    provider = register(
        project_name=project_name,
        auto_instrument=False,
        batch=False,
        protocol="http/protobuf",
        verbose=False,
        set_global_tracer_provider=False,
    )
    return cast("_Tracer", provider.get_tracer("pynchy.conversation"))


class PhoenixConversationStore:
    def __init__(self, *, tracer: _Tracer) -> None:
        self._tracer = tracer

    async def write_event(self, event: ConversationEvent) -> PhoenixEventRef:
        try:
            with self._tracer.start_as_current_span(
                event.span_name(),
                attributes=event.span_attributes(),
            ) as span:
                span.add_event(
                    "pynchy.conversation.body",
                    {"pynchy.content": event.content},
                )
        except Exception as exc:
            raise PhoenixWriteError(
                f"Failed to write conversation event {event.event_id} to Phoenix"
            ) from exc
        return PhoenixEventRef(
            event_id=event.event_id,
            trace_ref=f"phoenix:event:{event.event_id}",
        )
