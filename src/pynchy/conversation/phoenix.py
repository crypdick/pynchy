from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType  # noqa: TC003 - beartype resolves protocol annotations.
from typing import Protocol, Self, cast, runtime_checkable

from phoenix.client import Client
from phoenix.otel import register

from pynchy.conversation.events import (  # noqa: TC001 - beartype resolves annotations.
    ConversationEvent,
)


class PhoenixWriteError(RuntimeError):
    """Raised when Phoenix rejects or fails to record durable conversation content."""


class PhoenixReadError(RuntimeError):
    """Raised when Phoenix cannot provide durable conversation content."""


@dataclass(frozen=True, slots=True)
class PhoenixEventRef:
    event_id: str
    trace_ref: str


class ConversationBodyStore(Protocol):
    async def write_event(self, event: ConversationEvent) -> PhoenixEventRef: ...


class ConversationBodyReader(Protocol):
    async def read_event_content(self, event_id: str) -> str: ...


class _PhoenixSpans(Protocol):
    def get_spans(
        self,
        *,
        project_identifier: str,
        attributes: dict[str, str],
        limit: int,
    ) -> Sequence[Mapping[str, object]]: ...


class _PhoenixClient(Protocol):
    spans: _PhoenixSpans


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


def _phoenix_client_base_url(endpoint: str | None) -> str | None:
    base_url = endpoint.strip().rstrip("/") if endpoint is not None else ""
    if base_url.endswith("/v1/traces"):
        base_url = base_url.removesuffix("/v1/traces").rstrip("/")
    return base_url or None


def _span_content(span: Mapping[str, object]) -> str | None:
    attributes = span.get("attributes")
    if isinstance(attributes, Mapping):
        content = attributes.get("pynchy.content")
        if isinstance(content, str):
            return content

    events = span.get("events")
    if isinstance(events, Sequence) and not isinstance(events, str | bytes):
        for event in events:
            if not isinstance(event, Mapping):
                continue
            if event.get("name") != "pynchy.conversation.body":
                continue
            event_attributes = event.get("attributes")
            if not isinstance(event_attributes, Mapping):
                continue
            content = event_attributes.get("pynchy.content")
            if isinstance(content, str):
                return content
    return None


class PhoenixConversationBodyReader:
    def __init__(
        self,
        *,
        project_name: str,
        endpoint: str | None = None,
        client: _PhoenixClient | None = None,
    ) -> None:
        self._project_name = project_name
        self._client = client or cast(
            "_PhoenixClient",
            Client(base_url=_phoenix_client_base_url(endpoint)),
        )

    async def read_event_content(self, event_id: str) -> str:
        try:
            spans = await asyncio.to_thread(
                self._client.spans.get_spans,
                project_identifier=self._project_name,
                attributes={"pynchy.event_id": event_id},
                limit=1,
            )
        except Exception as exc:
            raise PhoenixReadError(
                f"Failed to read conversation event {event_id} from Phoenix"
            ) from exc

        for span in spans:
            content = _span_content(span)
            if content is not None:
                return content
        raise PhoenixReadError(f"Phoenix content missing for conversation event {event_id}")


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
