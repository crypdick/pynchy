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
    trace_id: str | None = None
    span_id: str | None = None


class ConversationBodyStore(Protocol):
    async def write_event(self, event: ConversationEvent) -> PhoenixEventRef: ...


class ConversationBodyReader(Protocol):
    async def read_event_content(self, event_id: str, *, phoenix_ref: str | None = None) -> str: ...


class _PhoenixSpans(Protocol):
    def get_spans(self, **kwargs: object) -> Sequence[Mapping[str, object]]: ...


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

    def get_span_context(self) -> object: ...


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


def _hex_id(value: object, width: int) -> str | None:
    if isinstance(value, int) and value > 0:
        return f"{value:0{width}x}"
    if isinstance(value, str) and value:
        return value
    return None


def _span_context_ids(span: object) -> tuple[str | None, str | None]:
    get_context = getattr(span, "get_span_context", None)
    if not callable(get_context):
        return None, None
    context = get_context()
    trace_id = _hex_id(getattr(context, "trace_id", None), 32)
    span_id = _hex_id(getattr(context, "span_id", None), 16)
    return trace_id, span_id


def _trace_ref(event_id: str, trace_id: str | None, span_id: str | None) -> str:
    if not trace_id or not span_id:
        raise PhoenixWriteError(f"Phoenix span context missing for conversation event {event_id}")
    return f"phoenix:trace:{trace_id}:span:{span_id}:event:{event_id}"


def _parse_trace_ref(ref: str | None) -> tuple[str | None, str | None]:
    if not ref:
        return None, None
    parts = ref.split(":")
    if (
        len(parts) == 7
        and parts[0] == "phoenix"
        and parts[1] == "trace"
        and parts[3] == "span"
        and parts[5] == "event"
    ):
        return parts[2], parts[4]
    return None, None


def _span_identifier(span: Mapping[str, object]) -> str | None:
    context = span.get("context")
    if isinstance(context, Mapping):
        span_id = context.get("span_id")
        if isinstance(span_id, str):
            return span_id
    span_id = span.get("span_id")
    return span_id if isinstance(span_id, str) else None


def _span_matches_event(span: Mapping[str, object], *, event_id: str, span_id: str | None) -> bool:
    if span_id and _span_identifier(span) == span_id:
        return True
    attributes = span.get("attributes")
    return isinstance(attributes, Mapping) and attributes.get("pynchy.event_id") == event_id


class PhoenixConversationBodyReader:
    def __init__(
        self,
        *,
        project_name: str,
        endpoint: str | None = None,
        client: _PhoenixClient | None = None,
        read_timeout_seconds: int = 30,
    ) -> None:
        self._project_name = project_name
        self._read_timeout_seconds = read_timeout_seconds
        self._client = client or cast(
            "_PhoenixClient",
            Client(base_url=_phoenix_client_base_url(endpoint)),
        )

    async def read_event_content(self, event_id: str, *, phoenix_ref: str | None = None) -> str:
        trace_id, span_id = _parse_trace_ref(phoenix_ref)
        if trace_id is None:
            raise PhoenixReadError(
                f"Phoenix ref for conversation event {event_id} does not include trace_id"
            )
        try:
            spans = await asyncio.to_thread(
                self._client.spans.get_spans,
                project_identifier=self._project_name,
                trace_ids=[trace_id],
                limit=100,
                timeout=self._read_timeout_seconds,
            )
        except Exception as exc:
            raise PhoenixReadError(
                f"Failed to read conversation event {event_id} from Phoenix"
            ) from exc

        for span in spans:
            if not _span_matches_event(span, event_id=event_id, span_id=span_id):
                continue
            content = _span_content(span)
            if content is not None:
                return content
        raise PhoenixReadError(f"Phoenix content missing for conversation event {event_id}")


class PhoenixConversationStore:
    def __init__(self, *, tracer: _Tracer) -> None:
        self._tracer = tracer

    async def write_event(self, event: ConversationEvent) -> PhoenixEventRef:
        trace_id: str | None = None
        span_id: str | None = None
        try:
            with self._tracer.start_as_current_span(
                event.span_name(),
                attributes=event.span_attributes(),
            ) as span:
                span.add_event(
                    "pynchy.conversation.body",
                    {"pynchy.content": event.content},
                )
                trace_id, span_id = _span_context_ids(span)
        except Exception as exc:
            raise PhoenixWriteError(
                f"Failed to write conversation event {event.event_id} to Phoenix"
            ) from exc
        return PhoenixEventRef(
            event_id=event.event_id,
            trace_ref=_trace_ref(event.event_id, trace_id, span_id),
            trace_id=trace_id,
            span_id=span_id,
        )
