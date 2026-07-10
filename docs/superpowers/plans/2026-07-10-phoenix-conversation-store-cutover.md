# Phoenix Conversation Store Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store every new channel-visible conversation event in Phoenix first, then keep SQLite as a local pointer/index projection used for ordering and unread-turn discovery.

**Architecture:** Add a small conversation-store boundary that writes durable event bodies to Phoenix through Phoenix OTEL manual spans, then inserts a SQLite projection row only after Phoenix accepts the event. Existing message-read APIs hydrate projection rows back into `NewMessage` objects so the orchestrator can keep using the current queue and prompt-preparation flow while the `messages.content` column becomes legacy-only. Turn IDs are generated at turn start, written into host events, passed into agent containers, and attached to supported model requests so LiteLLM Phoenix spans and host conversation spans can be joined.

**Tech Stack:** Python 3.13, aiosqlite, aiohttp, `arize-phoenix-otel>=0.16.0`, OpenTelemetry/OpenInference span attributes, pytest/pytest-asyncio, uv.

---

## File Structure

- Create `src/pynchy/conversation/events.py`: semantic conversation event dataclasses, ID generation, preview generation, and conversion from `NewMessage`.
- Create `src/pynchy/conversation/phoenix.py`: Phoenix writer using `phoenix.otel.register()` and manual spans; exposes a fakeable `ConversationBodyStore` protocol.
- Create `src/pynchy/conversation/sink.py`: ordered write boundary; Phoenix write succeeds first, SQLite projection second.
- Create `src/pynchy/state/conversation_events.py`: SQLite projection CRUD and hydration helpers.
- Modify `src/pynchy/state/schema.py`: add `conversation_events` projection table and indexes.
- Modify `src/pynchy/state/messages.py`: keep old storage API for legacy tests/admin paths, but make reads merge legacy `messages` with hydrated Phoenix projection rows.
- Modify `src/pynchy/types.py` and `src/pynchy/agent/agent_runner/src/agent_runner/models.py`: add `turn_id` to container input.
- Modify `src/pynchy/host/container_manager/serialization.py`: serialize `turn_id`.
- Modify `src/pynchy/host/orchestrator/agent_runner.py`: generate/pass turn IDs and agent-core metadata.
- Modify `src/pynchy/agent/agent_runner/src/agent_runner/core.py`: add `turn_id` to `AgentCoreConfig`.
- Modify `src/pynchy/agent/agent_runner/src/agent_runner/main.py`: pass `turn_id` into core config.
- Modify `src/pynchy/agent/agent_runner/src/agent_runner/cores/openai.py`: pass metadata to `Runner.run_streamed` when supported.
- Modify `src/pynchy/host/orchestrator/app.py`, `messaging/pipeline.py`, and `messaging/router.py`: replace new content writes with the sink.
- Modify `src/pynchy/config/models.py`, `src/pynchy/config/settings.py`, and sample docs/config: add conversation-store config and env aliases.
- Test files: `tests/test_conversation_events.py`, `tests/test_phoenix_conversation_store.py`, `tests/test_conversation_sink.py`, `tests/test_state_conversation_events.py`, plus focused updates to existing state/router/pipeline/agent-runner tests.

## Task 1: Add Conversation Event Domain Model

**Files:**
- Create: `src/pynchy/conversation/__init__.py`
- Create: `src/pynchy/conversation/events.py`
- Test: `tests/test_conversation_events.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_conversation_events.py
from __future__ import annotations

from pynchy.conversation.events import (
    ConversationEvent,
    ConversationEventKind,
    content_preview,
    new_event_id,
    new_turn_id,
)


def test_new_ids_are_prefixed_and_distinct() -> None:
    assert new_turn_id().startswith("turn_")
    assert new_event_id().startswith("evt_")
    assert new_turn_id() != new_turn_id()


def test_content_preview_collapses_whitespace_and_marks_truncation() -> None:
    text = "alpha\n\nbeta\tgamma " + ("x" * 600)
    preview = content_preview(text, limit=32)
    assert preview == "alpha beta gamma xxxxxxxxxxxxxx..."
    assert len(preview) == 35


def test_conversation_event_metadata_omits_empty_values() -> None:
    event = ConversationEvent(
        event_id="evt_1",
        turn_id="turn_1",
        chat_jid="slack:C123",
        timestamp="2026-07-10T00:00:00+00:00",
        kind=ConversationEventKind.USER_MESSAGE,
        sender="alice",
        sender_name=None,
        content="hello",
        message_type="user",
        source_message_id=None,
        metadata={"slack_ts": "1.23"},
    )
    attrs = event.span_attributes()
    assert attrs["pynchy.event_id"] == "evt_1"
    assert attrs["pynchy.turn_id"] == "turn_1"
    assert attrs["pynchy.kind"] == "user_message"
    assert attrs["pynchy.chat_jid"] == "slack:C123"
    assert attrs["pynchy.sender"] == "alice"
    assert "pynchy.sender_name" not in attrs
    assert attrs["pynchy.metadata_json"] == '{"slack_ts":"1.23"}'
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_conversation_events.py -q --no-cov
```

Expected: import failure for `pynchy.conversation.events`.

- [ ] **Step 3: Implement the domain model**

```python
# src/pynchy/conversation/__init__.py
"""Conversation content storage and projection helpers."""
```

```python
# src/pynchy/conversation/events.py
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ConversationEventKind(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    HOST_MESSAGE = "host_message"
    SYSTEM_NOTICE = "system_notice"


def new_turn_id() -> str:
    return f"turn_{secrets.token_urlsafe(18)}"


def new_event_id() -> str:
    return f"evt_{secrets.token_urlsafe(18)}"


_WHITESPACE = re.compile(r"\s+")


def content_preview(content: str, *, limit: int = 500) -> str:
    normalized = _WHITESPACE.sub(" ", content).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    event_id: str
    turn_id: str
    chat_jid: str
    timestamp: str
    kind: ConversationEventKind
    sender: str
    sender_name: str | None
    content: str
    message_type: str
    source_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def preview(self) -> str:
        return content_preview(self.content)

    def span_name(self) -> str:
        return f"pynchy.conversation.{self.kind.value}"

    def span_attributes(self) -> dict[str, object]:
        attrs: dict[str, object] = {
            "pynchy.event_id": self.event_id,
            "pynchy.turn_id": self.turn_id,
            "pynchy.chat_jid": self.chat_jid,
            "pynchy.kind": self.kind.value,
            "pynchy.sender": self.sender,
            "pynchy.message_type": self.message_type,
            "pynchy.content": self.content,
            "pynchy.content_preview": self.preview,
        }
        if self.sender_name:
            attrs["pynchy.sender_name"] = self.sender_name
        if self.source_message_id:
            attrs["pynchy.source_message_id"] = self.source_message_id
        if self.metadata:
            attrs["pynchy.metadata_json"] = json.dumps(
                self.metadata,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        return attrs
```

- [ ] **Step 4: Run the tests and commit**

Run:

```bash
uv run pytest tests/test_conversation_events.py -q --no-cov
git add src/pynchy/conversation/__init__.py src/pynchy/conversation/events.py tests/test_conversation_events.py
git commit -m "feat: add conversation event model"
```

Expected: tests pass; commit succeeds.

## Task 2: Add Phoenix Body Store

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `src/pynchy/conversation/phoenix.py`
- Test: `tests/test_phoenix_conversation_store.py`

- [ ] **Step 1: Add the official Phoenix OTEL dependency**

Run:

```bash
uv add "arize-phoenix-otel>=0.16.0"
```

Expected: `pyproject.toml` includes `arize-phoenix-otel>=0.16.0` and `uv.lock` changes.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_phoenix_conversation_store.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pynchy.conversation.events import ConversationEvent, ConversationEventKind
from pynchy.conversation.phoenix import PhoenixConversationStore, PhoenixWriteError


@dataclass
class StartedSpan:
    name: str
    attributes: dict[str, object] | None = None
    events: list[tuple[str, dict[str, object]]] | None = None

    def __enter__(self) -> "StartedSpan":
        self.events = []
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def add_event(self, name: str, attributes: dict[str, object]) -> None:
        assert self.events is not None
        self.events.append((name, attributes))


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[StartedSpan] = []

    def start_as_current_span(
        self, name: str, *, attributes: dict[str, object]
    ) -> StartedSpan:
        span = StartedSpan(name=name, attributes=attributes)
        self.spans.append(span)
        return span


class FailingTracer(FakeTracer):
    def start_as_current_span(self, name: str, *, attributes: dict[str, object]) -> StartedSpan:
        raise RuntimeError("phoenix offline")


def _event() -> ConversationEvent:
    return ConversationEvent(
        event_id="evt_1",
        turn_id="turn_1",
        chat_jid="slack:C123",
        timestamp="2026-07-10T00:00:00+00:00",
        kind=ConversationEventKind.USER_MESSAGE,
        sender="alice",
        sender_name="Alice",
        content="hello",
        message_type="user",
    )


async def test_write_event_returns_phoenix_ref() -> None:
    tracer = FakeTracer()
    store = PhoenixConversationStore(tracer=tracer)
    ref = await store.write_event(_event())
    assert ref.trace_ref == "phoenix:event:evt_1"
    assert ref.event_id == "evt_1"
    assert tracer.spans[0].name == "pynchy.conversation.user_message"
    assert tracer.spans[0].attributes["pynchy.content"] == "hello"
    assert tracer.spans[0].events == [
        ("pynchy.conversation.body", {"pynchy.content": "hello"})
    ]


async def test_write_event_wraps_tracer_failures() -> None:
    store = PhoenixConversationStore(tracer=FailingTracer())
    with pytest.raises(PhoenixWriteError, match="Failed to write conversation event evt_1"):
        await store.write_event(_event())
```

- [ ] **Step 3: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_phoenix_conversation_store.py -q --no-cov
```

Expected: import failure for `pynchy.conversation.phoenix`.

- [ ] **Step 4: Implement the Phoenix writer**

```python
# src/pynchy/conversation/phoenix.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from phoenix.otel import register

from pynchy.conversation.events import ConversationEvent


class PhoenixWriteError(RuntimeError):
    """Raised when Phoenix rejects or fails to record durable conversation content."""


@dataclass(frozen=True, slots=True)
class PhoenixEventRef:
    event_id: str
    trace_ref: str


class ConversationBodyStore(Protocol):
    async def write_event(self, event: ConversationEvent) -> PhoenixEventRef: ...


class _Tracer(Protocol):
    def start_as_current_span(
        self, name: str, *, attributes: dict[str, object]
    ) -> object: ...


def phoenix_tracer(*, project_name: str, endpoint: str | None = None) -> object:
    kwargs: dict[str, object] = {
        "project_name": project_name,
        "auto_instrument": False,
        "batch": False,
        "protocol": "http/protobuf",
    }
    if endpoint:
        kwargs["endpoint"] = endpoint
    provider = register(**kwargs)
    return provider.get_tracer("pynchy.conversation")


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
        except Exception as exc:  # noqa: BLE001
            raise PhoenixWriteError(
                f"Failed to write conversation event {event.event_id} to Phoenix"
            ) from exc
        return PhoenixEventRef(event_id=event.event_id, trace_ref=f"phoenix:event:{event.event_id}")
```

- [ ] **Step 5: Run the tests and commit**

Run:

```bash
uv run pytest tests/test_phoenix_conversation_store.py -q --no-cov
git add pyproject.toml uv.lock src/pynchy/conversation/phoenix.py tests/test_phoenix_conversation_store.py
git commit -m "feat: add Phoenix conversation body store"
```

Expected: tests pass; commit succeeds.

## Task 3: Add SQLite Projection Table

**Files:**
- Modify: `src/pynchy/state/schema.py`
- Create: `src/pynchy/state/conversation_events.py`
- Modify: `src/pynchy/state/__init__.py`
- Test: `tests/test_state_conversation_events.py`

- [ ] **Step 1: Write failing projection tests**

```python
# tests/test_state_conversation_events.py
from __future__ import annotations

from pynchy.conversation.events import ConversationEvent, ConversationEventKind
from pynchy.conversation.phoenix import PhoenixEventRef
from pynchy.state import (
    get_conversation_event_pointers_since,
    init_test_database,
    store_conversation_event_pointer,
)


def _event(event_id: str, timestamp: str) -> ConversationEvent:
    return ConversationEvent(
        event_id=event_id,
        turn_id="turn_1",
        chat_jid="slack:C123",
        timestamp=timestamp,
        kind=ConversationEventKind.USER_MESSAGE,
        sender="alice",
        sender_name="Alice",
        content=f"body {event_id}",
        message_type="user",
        metadata={"source": "test"},
    )


async def test_store_and_load_projection_pointer(tmp_path) -> None:
    await init_test_database(tmp_path / "messages.db")
    event = _event("evt_1", "2026-07-10T00:00:00+00:00")
    await store_conversation_event_pointer(event, PhoenixEventRef("evt_1", "phoenix:event:evt_1"))
    rows = await get_conversation_event_pointers_since("slack:C123", None)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "evt_1"
    assert rows[0]["content_preview"] == "body evt_1"
    assert rows[0]["phoenix_ref"] == "phoenix:event:evt_1"
    assert rows[0]["metadata"] == {"source": "test"}


async def test_since_filter_is_exclusive(tmp_path) -> None:
    await init_test_database(tmp_path / "messages.db")
    await store_conversation_event_pointer(
        _event("evt_1", "2026-07-10T00:00:00+00:00"),
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )
    await store_conversation_event_pointer(
        _event("evt_2", "2026-07-10T00:01:00+00:00"),
        PhoenixEventRef("evt_2", "phoenix:event:evt_2"),
    )
    rows = await get_conversation_event_pointers_since(
        "slack:C123", "2026-07-10T00:00:00+00:00"
    )
    assert [row["event_id"] for row in rows] == ["evt_2"]
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_state_conversation_events.py -q --no-cov
```

Expected: missing state functions.

- [ ] **Step 3: Add schema and CRUD implementation**

Add this table to `_SCHEMA` in `src/pynchy/state/schema.py`:

```python
conversation_events = """
CREATE TABLE IF NOT EXISTS conversation_events (
    event_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,
    sender TEXT NOT NULL,
    sender_name TEXT,
    message_type TEXT NOT NULL,
    source_message_id TEXT,
    content_preview TEXT NOT NULL,
    phoenix_ref TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_conversation_events_chat_time
ON conversation_events(chat_jid, timestamp);
CREATE INDEX IF NOT EXISTS idx_conversation_events_turn
ON conversation_events(turn_id);
"""
```

Create the module:

```python
# src/pynchy/state/conversation_events.py
from __future__ import annotations

import json
from typing import Any

from pynchy.conversation.events import ConversationEvent
from pynchy.conversation.phoenix import PhoenixEventRef
from pynchy.state.database import connection


async def store_conversation_event_pointer(
    event: ConversationEvent, ref: PhoenixEventRef
) -> None:
    async with connection() as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO conversation_events (
                event_id, turn_id, chat_jid, timestamp, kind, sender, sender_name,
                message_type, source_message_id, content_preview, phoenix_ref, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.turn_id,
                event.chat_jid,
                event.timestamp,
                event.kind.value,
                event.sender,
                event.sender_name,
                event.message_type,
                event.source_message_id,
                event.preview,
                ref.trace_ref,
                json.dumps(event.metadata, sort_keys=True, separators=(",", ":"), default=str),
            ),
        )
        await db.commit()


def _decode_metadata(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {}


async def get_conversation_event_pointers_since(
    chat_jid: str,
    since_timestamp: str | None,
) -> list[dict[str, Any]]:
    query = """
        SELECT * FROM conversation_events
        WHERE chat_jid = ?
    """
    params: list[object] = [chat_jid]
    if since_timestamp is not None:
        query += " AND timestamp > ?"
        params.append(since_timestamp)
    query += " ORDER BY timestamp ASC, event_id ASC"

    async with connection() as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["metadata"] = _decode_metadata(row.get("metadata"))
    return result
```

Export from `src/pynchy/state/__init__.py`:

```python
from pynchy.state.conversation_events import (
    get_conversation_event_pointers_since,
    store_conversation_event_pointer,
)
```

- [ ] **Step 4: Run the tests and commit**

Run:

```bash
uv run pytest tests/test_state_conversation_events.py -q --no-cov
git add src/pynchy/state/schema.py src/pynchy/state/conversation_events.py src/pynchy/state/__init__.py tests/test_state_conversation_events.py
git commit -m "feat: add conversation event projection"
```

Expected: tests pass; commit succeeds.

## Task 4: Add Ordered Conversation Sink

**Files:**
- Create: `src/pynchy/conversation/sink.py`
- Test: `tests/test_conversation_sink.py`

- [ ] **Step 1: Write failing sink tests**

```python
# tests/test_conversation_sink.py
from __future__ import annotations

import pytest

from pynchy.conversation.events import ConversationEvent, ConversationEventKind
from pynchy.conversation.phoenix import PhoenixEventRef, PhoenixWriteError
from pynchy.conversation.sink import ConversationSink


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.writes: list[ConversationEvent] = []

    async def write_event(self, event: ConversationEvent) -> PhoenixEventRef:
        if self.fail:
            raise PhoenixWriteError("no phoenix")
        self.writes.append(event)
        return PhoenixEventRef(event.event_id, f"phoenix:event:{event.event_id}")


def _event() -> ConversationEvent:
    return ConversationEvent(
        event_id="evt_1",
        turn_id="turn_1",
        chat_jid="slack:C123",
        timestamp="2026-07-10T00:00:00+00:00",
        kind=ConversationEventKind.USER_MESSAGE,
        sender="alice",
        sender_name="Alice",
        content="hello",
        message_type="user",
    )


async def test_sink_writes_phoenix_before_projection() -> None:
    calls: list[str] = []

    async def store_pointer(event: ConversationEvent, ref: PhoenixEventRef) -> None:
        calls.append(f"sqlite:{event.event_id}:{ref.trace_ref}")

    store = FakeStore()
    sink = ConversationSink(body_store=store, store_pointer=store_pointer)
    ref = await sink.append(_event())
    assert ref.trace_ref == "phoenix:event:evt_1"
    assert [event.event_id for event in store.writes] == ["evt_1"]
    assert calls == ["sqlite:evt_1:phoenix:event:evt_1"]


async def test_sink_does_not_project_when_phoenix_fails() -> None:
    calls: list[str] = []

    async def store_pointer(event: ConversationEvent, ref: PhoenixEventRef) -> None:
        calls.append(event.event_id)

    sink = ConversationSink(body_store=FakeStore(fail=True), store_pointer=store_pointer)
    with pytest.raises(PhoenixWriteError):
        await sink.append(_event())
    assert calls == []
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_conversation_sink.py -q --no-cov
```

Expected: missing `pynchy.conversation.sink`.

- [ ] **Step 3: Implement the sink**

```python
# src/pynchy/conversation/sink.py
from __future__ import annotations

from collections.abc import Awaitable, Callable

from pynchy.conversation.events import ConversationEvent
from pynchy.conversation.phoenix import ConversationBodyStore, PhoenixEventRef
from pynchy.state.conversation_events import store_conversation_event_pointer


StorePointer = Callable[[ConversationEvent, PhoenixEventRef], Awaitable[None]]


class ConversationSink:
    def __init__(
        self,
        *,
        body_store: ConversationBodyStore,
        store_pointer: StorePointer = store_conversation_event_pointer,
    ) -> None:
        self._body_store = body_store
        self._store_pointer = store_pointer

    async def append(self, event: ConversationEvent) -> PhoenixEventRef:
        ref = await self._body_store.write_event(event)
        await self._store_pointer(event, ref)
        return ref
```

- [ ] **Step 4: Run the tests and commit**

Run:

```bash
uv run pytest tests/test_conversation_sink.py -q --no-cov
git add src/pynchy/conversation/sink.py tests/test_conversation_sink.py
git commit -m "feat: add conversation sink"
```

Expected: tests pass; commit succeeds.

## Task 5: Configure Phoenix Conversation Store

**Files:**
- Modify: `src/pynchy/config/models.py`
- Modify: `src/pynchy/config/settings.py`
- Create: `src/pynchy/conversation/factory.py`
- Test: `tests/test_conversation_store_config.py`

- [ ] **Step 1: Write failing config tests**

```python
# tests/test_conversation_store_config.py
from __future__ import annotations

from pynchy.conversation.factory import resolved_phoenix_endpoint


def test_endpoint_prefers_base_collector_endpoint() -> None:
    env = {
        "PHOENIX_COLLECTOR_ENDPOINT": "https://phoenix.example.com",
        "PHOENIX_COLLECTOR_HTTP_ENDPOINT": "https://wrong.example.com/v1/traces",
    }
    assert resolved_phoenix_endpoint(env) == "https://phoenix.example.com"


def test_endpoint_derives_base_from_litellm_http_endpoint() -> None:
    env = {"PHOENIX_COLLECTOR_HTTP_ENDPOINT": "https://phoenix.example.com/v1/traces"}
    assert resolved_phoenix_endpoint(env) == "https://phoenix.example.com"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_conversation_store_config.py -q --no-cov
```

Expected: missing factory module.

- [ ] **Step 3: Add config model and factory**

Add this model to `src/pynchy/config/models.py`:

```python
from pydantic import BaseModel


class ConversationStoreConfig(BaseModel):
    project_name: str = "pynchy"
    phoenix_endpoint: str | None = None
```

Add the settings field in `src/pynchy/config/settings.py`:

```python
from pynchy.config.models import ConversationStoreConfig


class Settings(BaseSettings):
    conversation_store: ConversationStoreConfig = ConversationStoreConfig()
```

Create the factory:

```python
# src/pynchy/conversation/factory.py
from __future__ import annotations

import os
from collections.abc import Mapping

from pynchy.config import get_settings
from pynchy.conversation.phoenix import PhoenixConversationStore, phoenix_tracer
from pynchy.conversation.sink import ConversationSink


def resolved_phoenix_endpoint(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    base = source.get("PHOENIX_COLLECTOR_ENDPOINT", "").strip()
    if base:
        return base.rstrip("/")
    http_endpoint = source.get("PHOENIX_COLLECTOR_HTTP_ENDPOINT", "").strip()
    if http_endpoint.endswith("/v1/traces"):
        return http_endpoint.removesuffix("/v1/traces").rstrip("/")
    return http_endpoint.rstrip("/") or None


def build_conversation_sink() -> ConversationSink:
    settings = get_settings().conversation_store
    endpoint = settings.phoenix_endpoint or resolved_phoenix_endpoint()
    tracer = phoenix_tracer(project_name=settings.project_name, endpoint=endpoint)
    return ConversationSink(body_store=PhoenixConversationStore(tracer=tracer))
```

- [ ] **Step 4: Run the tests and commit**

Run:

```bash
uv run pytest tests/test_conversation_store_config.py -q --no-cov
git add src/pynchy/config/models.py src/pynchy/config/settings.py src/pynchy/conversation/factory.py tests/test_conversation_store_config.py
git commit -m "feat: configure Phoenix conversation store"
```

Expected: tests pass; commit succeeds.

## Task 6: Hydrate Projection Rows Through Existing Message Reads

**Files:**
- Modify: `src/pynchy/state/messages.py`
- Modify: `src/pynchy/state/conversation_events.py`
- Test: `tests/test_state.py`
- Test: `tests/test_state_conversation_events.py`

- [ ] **Step 1: Add failing hydration tests**

Append to `tests/test_state_conversation_events.py`:

```python
from pynchy.state import get_messages_since


async def test_get_messages_since_includes_projected_conversation_events(tmp_path) -> None:
    await init_test_database(tmp_path / "messages.db")
    await store_conversation_event_pointer(
        _event("evt_1", "2026-07-10T00:00:00+00:00"),
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )
    messages = await get_messages_since("slack:C123", None)
    assert len(messages) == 1
    assert messages[0].id == "evt_1"
    assert messages[0].content == "body evt_1"
    assert messages[0].metadata["phoenix_ref"] == "phoenix:event:evt_1"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_state_conversation_events.py::test_get_messages_since_includes_projected_conversation_events -q --no-cov
```

Expected: `get_messages_since` ignores `conversation_events`.

- [ ] **Step 3: Add row conversion and merge reads**

In `src/pynchy/state/conversation_events.py`, add:

```python
from pynchy.types import NewMessage


def pointer_to_message(row: dict[str, Any]) -> NewMessage:
    metadata = dict(row.get("metadata") or {})
    metadata["phoenix_ref"] = row["phoenix_ref"]
    metadata["turn_id"] = row["turn_id"]
    return NewMessage(
        id=row["event_id"],
        chat_jid=row["chat_jid"],
        sender=row["sender"],
        sender_name=row.get("sender_name"),
        content=row["content_preview"],
        timestamp=row["timestamp"],
        is_from_me=row["message_type"] in {"assistant", "host"},
        message_type=row["message_type"],
        metadata=metadata,
    )
```

In `src/pynchy/state/messages.py`, update `get_messages_since` so it merges both sources:

```python
from pynchy.state.conversation_events import (
    get_conversation_event_pointers_since,
    pointer_to_message,
)


async def get_messages_since(chat_jid: str, since_timestamp: str | None) -> list[NewMessage]:
    legacy = await _get_legacy_messages_since(chat_jid, since_timestamp)
    projected_rows = await get_conversation_event_pointers_since(chat_jid, since_timestamp)
    projected = [pointer_to_message(row) for row in projected_rows]
    return sorted([*legacy, *projected], key=lambda msg: (msg.timestamp, msg.id))
```

Rename the current `get_messages_since` body to `_get_legacy_messages_since` without changing its SQL.

- [ ] **Step 4: Run state tests and commit**

Run:

```bash
uv run pytest tests/test_state.py tests/test_state_conversation_events.py -q --no-cov
git add src/pynchy/state/messages.py src/pynchy/state/conversation_events.py tests/test_state_conversation_events.py
git commit -m "feat: hydrate conversation projections in message reads"
```

Expected: tests pass; commit succeeds.

## Task 7: Wire New Inbound And Outbound Content Writes Through The Sink

**Files:**
- Modify: `src/pynchy/host/orchestrator/app.py`
- Modify: `src/pynchy/host/orchestrator/messaging/pipeline.py`
- Modify: `src/pynchy/host/orchestrator/messaging/router.py`
- Test: `tests/test_message_handler.py`
- Test: `tests/test_messaging_router.py`

- [ ] **Step 1: Add sink helper tests around direct command and final result writes**

Update router tests that patch `store_message_direct` to patch `conversation_sink.append` instead. Add one direct assertion:

```python
async def test_final_result_persists_assistant_event_through_conversation_sink(self):
    output = _make_output(type="result", result="Done", result_metadata=None)
    sink = AsyncMock()
    deps = _make_deps(conversation_sink=sink)
    handled = await handle_streamed_output(deps, "slack:C123", _group(), output)
    assert handled is True
    event = sink.append.call_args.args[0]
    assert event.kind.value == "assistant_message"
    assert event.chat_jid == "slack:C123"
    assert event.content == "Done"
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
uv run pytest tests/test_messaging_router.py tests/test_message_handler.py -q --no-cov
```

Expected: dependency objects do not expose a conversation sink yet.

- [ ] **Step 3: Add sink to orchestrator dependency objects**

Add a `conversation_sink` field wherever `OutputDeps` and pipeline deps are defined, then call it:

```python
from pynchy.conversation.events import ConversationEvent, ConversationEventKind, new_event_id


await deps.conversation_sink.append(
    ConversationEvent(
        event_id=new_event_id(),
        turn_id=deps.turn_id,
        chat_jid=chat_jid,
        timestamp=datetime.now(UTC).isoformat(),
        kind=ConversationEventKind.ASSISTANT_MESSAGE,
        sender="assistant",
        sender_name="Pynchy",
        content=result.result,
        message_type="assistant",
        metadata=result.result_metadata or {},
    )
)
```

Replace direct command output storage similarly, using `ConversationEventKind.HOST_MESSAGE` and `message_type="host"`. Replace host/system broadcaster storage wrappers in `app.py` with sink-backed wrappers using `HOST_MESSAGE` or `SYSTEM_NOTICE`.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest tests/test_messaging_router.py tests/test_message_handler.py -q --no-cov
git add src/pynchy/host/orchestrator/app.py src/pynchy/host/orchestrator/messaging/pipeline.py src/pynchy/host/orchestrator/messaging/router.py tests/test_messaging_router.py tests/test_message_handler.py
git commit -m "feat: write new conversation content through Phoenix sink"
```

Expected: tests pass; commit succeeds.

## Task 8: Add Turn IDs To Container Input And OpenAI Core Metadata

**Files:**
- Modify: `src/pynchy/types.py`
- Modify: `src/pynchy/host/container_manager/serialization.py`
- Modify: `src/pynchy/host/orchestrator/agent_runner.py`
- Modify: `src/pynchy/agent/agent_runner/src/agent_runner/models.py`
- Modify: `src/pynchy/agent/agent_runner/src/agent_runner/core.py`
- Modify: `src/pynchy/agent/agent_runner/src/agent_runner/main.py`
- Modify: `src/pynchy/agent/agent_runner/src/agent_runner/cores/openai.py`
- Test: `tests/test_container_runner.py`
- Test: `src/pynchy/agent/agent_runner/tests/test_agent_runner.py`

- [ ] **Step 1: Add failing turn ID serialization tests**

Add to `tests/test_container_runner.py`:

```python
def test_container_input_serializes_turn_id() -> None:
    data = ContainerInput(
        messages=[],
        group_folder="ops",
        chat_jid="slack:C123",
        is_admin=False,
        turn_id="turn_1",
    )
    encoded = input_to_dict(data)
    assert encoded["turn_id"] == "turn_1"
```

Add to `src/pynchy/agent/agent_runner/tests/test_agent_runner.py`:

```python
def test_container_input_accepts_turn_id() -> None:
    ci = ContainerInput.from_dict(
        {
            "messages": [],
            "group_folder": "ops",
            "chat_jid": "slack:C123",
            "is_admin": False,
            "turn_id": "turn_1",
        }
    )
    assert ci.turn_id == "turn_1"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_container_runner.py::test_container_input_serializes_turn_id src/pynchy/agent/agent_runner/tests/test_agent_runner.py::test_container_input_accepts_turn_id -q --no-cov
```

Expected: `turn_id` is not a known field.

- [ ] **Step 3: Add turn ID fields and request metadata**

Add `turn_id: str | None = None` to host and container `ContainerInput` dataclasses. Add `turn_id: str | None` to `AgentCoreConfig`. In `build_container_input`, generate/pass an existing turn ID:

```python
from pynchy.conversation.events import new_turn_id


turn_id = ctx.turn_id or new_turn_id()
agent_core_config = _agent_core_config_from_settings(group.folder) or {}
agent_core_config["metadata"] = {
    "pynchy_turn_id": turn_id,
    "pynchy_chat_jid": chat_jid,
    "pynchy_group_folder": group.folder,
}
return ContainerInput(..., turn_id=turn_id, agent_core_config=agent_core_config)
```

In OpenAI core:

```python
kwargs: dict[str, object] = {
    "previous_response_id": self._previous_response_id,
    "auto_previous_response_id": True,
}
if metadata := self.config.extra.get("metadata"):
    kwargs["metadata"] = metadata
result = Runner.run_streamed(agent, input=prompt, **kwargs)
```

If the installed Agents SDK rejects `metadata`, catch `TypeError` only around this call, remove `metadata`, log the downgrade, and rerun. Keep the turn ID on host-written Phoenix events either way.

- [ ] **Step 4: Run agent runner tests and commit**

Run:

```bash
uv run pytest tests/test_container_runner.py src/pynchy/agent/agent_runner/tests/test_agent_runner.py -q --no-cov
git add src/pynchy/types.py src/pynchy/host/container_manager/serialization.py src/pynchy/host/orchestrator/agent_runner.py src/pynchy/agent/agent_runner/src/agent_runner/models.py src/pynchy/agent/agent_runner/src/agent_runner/core.py src/pynchy/agent/agent_runner/src/agent_runner/main.py src/pynchy/agent/agent_runner/src/agent_runner/cores/openai.py tests/test_container_runner.py src/pynchy/agent/agent_runner/tests/test_agent_runner.py
git commit -m "feat: propagate turn ids to agent cores"
```

Expected: tests pass; commit succeeds.

## Task 9: Enforce No Phoenix, No Turn

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/inbound.py`
- Modify: `src/pynchy/host/orchestrator/messaging/pipeline.py`
- Modify: `src/pynchy/host/orchestrator/messaging/router.py`
- Test: `tests/test_message_handler.py`
- Test: `tests/test_messaging_router.py`

- [ ] **Step 1: Add failing failure-boundary tests**

Add to `tests/test_message_handler.py`:

```python
async def test_phoenix_write_failure_does_not_advance_cursor_or_start_agent(mocker) -> None:
    sink = AsyncMock()
    sink.append.side_effect = RuntimeError("phoenix down")
    deps = _make_processing_deps(conversation_sink=sink)
    deps.advance_cursor = AsyncMock()
    deps.run_agent = AsyncMock()
    result = await process_group_messages(deps, "slack:C123")
    assert result is False
    deps.advance_cursor.assert_not_called()
    deps.run_agent.assert_not_called()
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
uv run pytest tests/test_message_handler.py::test_phoenix_write_failure_does_not_advance_cursor_or_start_agent -q --no-cov
```

Expected: current pipeline starts the agent or advances cursor without checking Phoenix persistence for pending inbound events.

- [ ] **Step 3: Persist inbound events before queue finalization**

At the start of an interactive turn, convert pending channel messages to `ConversationEvent` instances and append them through the sink before `_finalize_cursor_and_retry`. Use the original channel message ID as `source_message_id`, preserve sender metadata, and use the same `turn_id` that will be passed to the container.

```python
for msg in pending_messages:
    await deps.conversation_sink.append(
        ConversationEvent(
            event_id=msg.id or new_event_id(),
            turn_id=turn_id,
            chat_jid=msg.chat_jid,
            timestamp=msg.timestamp,
            kind=ConversationEventKind.USER_MESSAGE,
            sender=msg.sender,
            sender_name=msg.sender_name,
            content=msg.content,
            message_type=msg.message_type,
            source_message_id=msg.id,
            metadata=msg.metadata or {},
        )
    )
```

Let exceptions propagate to the caller that marks processing failed. Do not store a legacy SQLite body, do not advance the channel cursor, and do not run the agent when this append fails.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest tests/test_message_handler.py tests/test_messaging_router.py -q --no-cov
git add src/pynchy/host/orchestrator/messaging/inbound.py src/pynchy/host/orchestrator/messaging/pipeline.py src/pynchy/host/orchestrator/messaging/router.py tests/test_message_handler.py tests/test_messaging_router.py
git commit -m "feat: require Phoenix before processing turns"
```

Expected: focused tests pass; commit succeeds.

## Task 10: Documentation And Verification

**Files:**
- Modify: `docs/architecture/message-routing.md`
- Modify: `docs/architecture/index.md`
- Modify: `docs/superpowers/specs/2026-07-10-phoenix-conversation-store-design.md`

- [ ] **Step 1: Update architecture docs**

Replace the old SQLite/Phoenix split wording with:

```markdown
For new events, Phoenix is the durable content store for user messages,
assistant replies, host notices, and system notices. SQLite stores a local
projection containing event IDs, turn IDs, timestamps, previews, sender metadata,
and Phoenix references so the host can order pending work without owning the
message bodies.

LiteLLM still writes model-call spans directly to Phoenix. Pynchy writes the
host-visible conversation events and passes `pynchy_turn_id` metadata into agent
cores where supported so Phoenix can join host conversation spans with LiteLLM
model spans.

There is no SQLite body failover for new events. If Phoenix cannot accept the
event, Pynchy does not insert the SQLite projection row, does not advance the
channel cursor, and does not start the agent turn.
```

- [ ] **Step 2: Run formatting, linting, typing, and focused tests**

Run:

```bash
uv run pytest tests/test_conversation_events.py tests/test_phoenix_conversation_store.py tests/test_conversation_sink.py tests/test_state_conversation_events.py tests/test_state.py tests/test_message_handler.py tests/test_messaging_router.py tests/test_container_runner.py src/pynchy/agent/agent_runner/tests/test_agent_runner.py -q --no-cov
uvx ruff check src tests
uv run mypy src
uv run mkdocs build --strict
```

Expected: all commands pass. The docs build may print existing non-fatal warnings; it must exit `0`.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
uv run pytest
```

Expected: exits `0`.

- [ ] **Step 4: Commit documentation and verification**

Run:

```bash
git add docs/architecture/message-routing.md docs/architecture/index.md docs/superpowers/specs/2026-07-10-phoenix-conversation-store-design.md
git commit -m "docs: document Phoenix conversation content source"
```

Expected: commit succeeds.

## Self-Review Checklist

- Spec coverage: Phoenix-first writes are covered by Tasks 2, 4, 7, and 9. SQLite pointer projection is covered by Tasks 3 and 6. No backfill and no failover are explicitly enforced by Task 9. LiteLLM-direct model spans remain intact, with turn correlation added in Task 8. Docs are updated in Task 10.
- Placeholder scan: The plan contains no deferred implementation markers and every code-changing task includes concrete code or exact replacement snippets.
- Type consistency: `ConversationEvent`, `PhoenixEventRef`, `ConversationSink.append`, `turn_id`, and `metadata` names are consistent across tasks.
