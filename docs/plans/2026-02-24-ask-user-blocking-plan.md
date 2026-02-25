# Ask-User Blocking Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable agents to ask users questions mid-task and block until they answer, routing questions through Slack Block Kit widgets (with WhatsApp numbered-text fallback).

**Architecture:** Custom MCP tool replaces the built-in `AskUserQuestion`. Container-side watchdog on `ipc/responses/` replaces polling. Host stores pending question state as files (matching the approval gate pattern). Channel plugins render interactive widgets and route answers back through IPC.

**Tech Stack:** Python, watchdog, slack-bolt (Block Kit), MCP (mcp library), file-based IPC

---

### Task 1: Upgrade `_ipc_request.py` — Replace Polling with Watchdog

This is shared infrastructure that benefits both ask-user and the existing approval gate.

**Files:**
- Modify: `container/agent_runner/src/agent_runner/agent_tools/_ipc_request.py`
- Test: `container/agent_runner/tests/test_ipc_request_watchdog.py`

**Step 1: Write the failing test**

```python
"""Tests for watchdog-based IPC response waiting."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def ipc_responses_dir(tmp_path):
    responses = tmp_path / "ipc" / "responses"
    responses.mkdir(parents=True)
    with patch("agent_runner.agent_tools._ipc_request.RESPONSES_DIR", responses):
        yield responses


@pytest.fixture
def ipc_tasks_dir(tmp_path):
    tasks = tmp_path / "ipc" / "tasks"
    tasks.mkdir(parents=True)
    with patch("agent_runner.agent_tools._ipc.IPC_DIR", tmp_path / "ipc"):
        yield tasks


@pytest.mark.asyncio
async def test_watchdog_picks_up_response(ipc_responses_dir, ipc_tasks_dir):
    """Response file created after request should unblock immediately."""
    from agent_runner.agent_tools._ipc_request import ipc_service_request

    async def write_response_after_delay():
        await asyncio.sleep(0.2)
        # Simulate host writing response (atomic rename)
        request_files = list(ipc_tasks_dir.glob("*.json"))
        assert len(request_files) == 1
        data = json.loads(request_files[0].read_text())
        rid = data["request_id"]
        resp_path = ipc_responses_dir / f"{rid}.json"
        tmp = resp_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"result": {"answer": "JWT tokens"}}))
        tmp.rename(resp_path)

    task = asyncio.create_task(write_response_after_delay())
    result = await ipc_service_request("ask_user", {"question": "which auth?"}, timeout=5)
    await task

    assert len(result) == 1
    assert "JWT tokens" in result[0].text


@pytest.mark.asyncio
async def test_watchdog_timeout(ipc_responses_dir, ipc_tasks_dir):
    """Should return timeout error when no response arrives."""
    from agent_runner.agent_tools._ipc_request import ipc_service_request

    result = await ipc_service_request("test_tool", {}, timeout=1)
    assert len(result) == 1
    assert "timed out" in result[0].text.lower()


@pytest.mark.asyncio
async def test_response_file_cleaned_up(ipc_responses_dir, ipc_tasks_dir):
    """Response file should be deleted after reading."""
    from agent_runner.agent_tools._ipc_request import ipc_service_request

    async def write_response_after_delay():
        await asyncio.sleep(0.2)
        request_files = list(ipc_tasks_dir.glob("*.json"))
        data = json.loads(request_files[0].read_text())
        rid = data["request_id"]
        resp = ipc_responses_dir / f"{rid}.json"
        resp.write_text(json.dumps({"result": {"ok": True}}))

    task = asyncio.create_task(write_response_after_delay())
    await ipc_service_request("test_tool", {}, timeout=5)
    await task

    assert len(list(ipc_responses_dir.glob("*.json"))) == 0
```

**Step 2: Run test to verify it fails**

Run: `cd container/agent_runner && uv run pytest tests/test_ipc_request_watchdog.py -v`
Expected: FAIL (tests use current polling implementation, but we'll be changing the internal mechanism)

**Step 3: Rewrite `_ipc_request.py` with watchdog**

```python
"""Request-response IPC for service tools.

Service tools write a request to the tasks/ directory and watch the
responses/ directory for the result using watchdog (no polling).
"""

from __future__ import annotations

import asyncio
import json
import uuid

from mcp.types import TextContent
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from agent_runner.agent_tools._ipc import IPC_DIR, write_ipc_file

RESPONSES_DIR = IPC_DIR / "responses"


class _ResponseWatcher(FileSystemEventHandler):
    """Watchdog handler that signals when a specific response file appears."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, event: asyncio.Event, request_id: str
    ) -> None:
        super().__init__()
        self._loop = loop
        self._event = event
        self._target = f"{request_id}.json"

    def _check(self, path_str: str) -> None:
        if path_str.endswith(self._target):
            self._loop.call_soon_threadsafe(self._event.set)

    def on_created(self, event: object) -> None:
        if isinstance(event, FileCreatedEvent):
            self._check(event.src_path)

    def on_moved(self, event: object) -> None:
        if isinstance(event, FileMovedEvent):
            self._check(event.dest_path)


async def ipc_service_request(
    tool_name: str,
    request: dict,
    timeout: float = 300,
) -> list[TextContent]:
    """Write an IPC service request and wait for the host's response.

    Uses watchdog to detect the response file instead of polling.
    """
    request_id = uuid.uuid4().hex
    request["type"] = f"service:{tool_name}"
    request["request_id"] = request_id

    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    write_ipc_file(IPC_DIR / "tasks", request)

    response_file = RESPONSES_DIR / f"{request_id}.json"

    # Check if response already exists (race: host responded before we watch)
    if response_file.exists():
        return _read_response(response_file)

    loop = asyncio.get_running_loop()
    wakeup = asyncio.Event()
    handler = _ResponseWatcher(loop, wakeup, request_id)
    observer = Observer()
    observer.schedule(handler, str(RESPONSES_DIR), recursive=False)
    observer.daemon = True
    observer.start()

    try:
        # Re-check after starting observer (close race window)
        if response_file.exists():
            return _read_response(response_file)

        await asyncio.wait_for(wakeup.wait(), timeout=timeout)
        return _read_response(response_file)
    except TimeoutError:
        return [TextContent(type="text", text="Error: Request timed out waiting for host response")]
    finally:
        observer.stop()
        observer.join(timeout=2)


def _read_response(response_file) -> list[TextContent]:
    """Read and delete a response file, returning MCP TextContent."""
    try:
        response = json.loads(response_file.read_text())
    finally:
        response_file.unlink(missing_ok=True)

    if response.get("error"):
        return [TextContent(type="text", text=f"Error: {response['error']}")]

    return [
        TextContent(
            type="text",
            text=json.dumps(response.get("result", {}), indent=2),
        )
    ]
```

**Step 4: Run test to verify it passes**

Run: `cd container/agent_runner && uv run pytest tests/test_ipc_request_watchdog.py -v`
Expected: PASS

**Step 5: Run existing tests to verify no regressions**

Run: `cd container/agent_runner && uv run pytest -v`
Expected: All existing tests still pass

**Step 6: Commit**

```bash
git add container/agent_runner/src/agent_runner/agent_tools/_ipc_request.py \
       container/agent_runner/tests/test_ipc_request_watchdog.py
git commit -m "feat(ipc): replace polling with watchdog for IPC response waiting"
```

---

### Task 2: Container-Side `ask_user` MCP Tool

**Files:**
- Create: `container/agent_runner/src/agent_runner/agent_tools/_tools_ask_user.py`
- Modify: `container/agent_runner/src/agent_runner/agent_tools/_server.py` (add import)
- Modify: `container/agent_runner/src/agent_runner/cores/claude.py` (disallow built-in)
- Test: `container/agent_runner/tests/test_ask_user_tool.py`

**Step 1: Write the failing test**

```python
"""Tests for the ask_user MCP tool."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def ipc_dirs(tmp_path):
    tasks = tmp_path / "ipc" / "tasks"
    responses = tmp_path / "ipc" / "responses"
    tasks.mkdir(parents=True)
    responses.mkdir(parents=True)
    with (
        patch("agent_runner.agent_tools._ipc.IPC_DIR", tmp_path / "ipc"),
        patch("agent_runner.agent_tools._ipc_request.IPC_DIR", tmp_path / "ipc"),
        patch("agent_runner.agent_tools._ipc_request.RESPONSES_DIR", responses),
    ):
        yield {"tasks": tasks, "responses": responses}


@pytest.mark.asyncio
async def test_ask_user_sends_task_and_returns_answer(ipc_dirs):
    from agent_runner.agent_tools._tools_ask_user import handle_ask_user

    async def simulate_answer():
        await asyncio.sleep(0.3)
        files = list(ipc_dirs["tasks"].glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["type"] == "ask_user:ask"
        assert len(data["questions"]) == 1
        assert data["questions"][0]["question"] == "Which framework?"

        rid = data["request_id"]
        resp = ipc_dirs["responses"] / f"{rid}.json"
        resp.write_text(json.dumps({
            "result": {"answers": {"Which framework?": "React"}}
        }))

    task = asyncio.create_task(simulate_answer())
    result = await handle_ask_user({
        "questions": [
            {
                "question": "Which framework?",
                "options": [
                    {"label": "React", "description": "Popular SPA framework"},
                    {"label": "Vue", "description": "Progressive framework"},
                ],
            }
        ]
    })
    await task

    assert len(result) == 1
    assert "React" in result[0].text


@pytest.mark.asyncio
async def test_ask_user_timeout(ipc_dirs):
    from agent_runner.agent_tools._tools_ask_user import handle_ask_user

    # Use a 1s timeout so test is fast
    with patch("agent_runner.agent_tools._tools_ask_user.ASK_USER_TIMEOUT", 1):
        result = await handle_ask_user({
            "questions": [{"question": "Test?", "options": []}]
        })

    assert len(result) == 1
    assert "timed out" in result[0].text.lower() or "did not respond" in result[0].text.lower()
```

**Step 2: Run test to verify it fails**

Run: `cd container/agent_runner && uv run pytest tests/test_ask_user_tool.py -v`
Expected: FAIL (module doesn't exist)

**Step 3: Create the ask_user tool module**

```python
"""MCP tool: ask_user — route questions to the user via messaging channels.

Replaces the built-in AskUserQuestion (which is a no-op in headless mode).
Questions are sent to the host, which routes them to Slack/WhatsApp as
interactive widgets. The tool blocks until the user responds or timeout.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from agent_runner.agent_tools._ipc_request import ipc_service_request
from agent_runner.agent_tools._registry import ToolEntry, register

# Timeout for user to respond (seconds). Container will be destroyed
# by the host after this if the user hasn't answered.
ASK_USER_TIMEOUT = 1800  # 30 minutes


async def handle_ask_user(arguments: dict) -> list[TextContent]:
    """Send questions to the user and block until they respond."""
    questions = arguments.get("questions", [])
    if not questions:
        return [TextContent(type="text", text="Error: No questions provided")]

    request = {"questions": questions}
    return await ipc_service_request("ask_user:ask", request, timeout=ASK_USER_TIMEOUT)


def _definition() -> Tool:
    return Tool(
        name="ask_user",
        description=(
            "Ask the user a question and wait for their response. "
            "Use this when you need clarification, want to present options, "
            "or need the user to make a decision before proceeding. "
            "The question will be sent to the user's messaging channel "
            "(Slack, WhatsApp, etc.) and you will receive their answer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Questions to ask the user (1-4 questions)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The question text",
                            },
                            "options": {
                                "type": "array",
                                "description": "Available choices (2-4 options)",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                        },
                        "required": ["question"],
                    },
                    "minItems": 1,
                    "maxItems": 4,
                }
            },
            "required": ["questions"],
        },
    )


register("ask_user", ToolEntry(definition=_definition, handler=handle_ask_user))
```

**Step 4: Add import to `_server.py`**

Add to the import block in `container/agent_runner/src/agent_runner/agent_tools/_server.py`:
```python
import agent_runner.agent_tools._tools_ask_user  # noqa: F401
```

**Step 5: Disallow built-in `AskUserQuestion`**

In `container/agent_runner/src/agent_runner/cores/claude.py`, change:
```python
disallowed_tools=["EnterPlanMode", "ExitPlanMode"],
```
to:
```python
disallowed_tools=["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"],
```

**Step 6: Run test to verify it passes**

Run: `cd container/agent_runner && uv run pytest tests/test_ask_user_tool.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add container/agent_runner/src/agent_runner/agent_tools/_tools_ask_user.py \
       container/agent_runner/src/agent_runner/agent_tools/_server.py \
       container/agent_runner/src/agent_runner/cores/claude.py \
       container/agent_runner/tests/test_ask_user_tool.py
git commit -m "feat(agent): add ask_user MCP tool, disallow built-in AskUserQuestion"
```

---

### Task 3: Host-Side Pending Question State Manager

Follows the approval gate pattern in `security/approval.py`.

**Files:**
- Create: `src/pynchy/chat/pending_questions.py`
- Test: `tests/test_pending_questions.py`

**Step 1: Write the failing test**

```python
"""Tests for pending question state management."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest


@pytest.fixture
def ipc_dir(tmp_path):
    with patch("pynchy.chat.pending_questions.get_settings") as mock_settings:
        mock_settings.return_value.data_dir = tmp_path
        yield tmp_path


def test_create_pending_question(ipc_dir):
    from pynchy.chat.pending_questions import create_pending_question

    create_pending_question(
        request_id="abc123",
        source_group="test-group",
        chat_jid="slack:C123",
        channel_name="slack",
        session_id="sess-456",
        questions=[{"question": "Which auth?", "options": []}],
    )

    pending_dir = ipc_dir / "ipc" / "test-group" / "pending_questions"
    files = list(pending_dir.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["request_id"] == "abc123"
    assert data["questions"][0]["question"] == "Which auth?"
    assert data["session_id"] == "sess-456"


def test_find_pending_by_request_id(ipc_dir):
    from pynchy.chat.pending_questions import (
        create_pending_question,
        find_pending_question,
    )

    create_pending_question(
        request_id="abc123",
        source_group="test-group",
        chat_jid="slack:C123",
        channel_name="slack",
        session_id="sess-456",
        questions=[{"question": "Which auth?", "options": []}],
    )

    result = find_pending_question("abc123")
    assert result is not None
    assert result["request_id"] == "abc123"


def test_find_pending_returns_none_when_missing(ipc_dir):
    from pynchy.chat.pending_questions import find_pending_question

    assert find_pending_question("nonexistent") is None


def test_resolve_pending_question_deletes_file(ipc_dir):
    from pynchy.chat.pending_questions import (
        create_pending_question,
        find_pending_question,
        resolve_pending_question,
    )

    create_pending_question(
        request_id="abc123",
        source_group="test-group",
        chat_jid="slack:C123",
        channel_name="slack",
        session_id="sess-456",
        questions=[{"question": "Which auth?", "options": []}],
    )

    resolve_pending_question("abc123", "test-group")
    assert find_pending_question("abc123") is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pending_questions.py -v`
Expected: FAIL (module doesn't exist)

**Step 3: Implement `pending_questions.py`**

```python
"""File-backed pending question state manager.

Manages pending question files in ipc/{group}/pending_questions/.
Follows the same pattern as security/approval.py (pending_approvals/).

    agent calls ask_user MCP tool
        → host writes pending_questions/{request_id}.json
        → host sends interactive widget to channel
        → container blocks (no response file written)

    user clicks button / replies
        → host writes ipc/responses/{request_id}.json
        → host deletes pending_questions/{request_id}.json
        → container unblocks
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger


def _pending_questions_dir(source_group: str) -> Path:
    d = get_settings().data_dir / "ipc" / source_group / "pending_questions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_pending_question(
    request_id: str,
    source_group: str,
    chat_jid: str,
    channel_name: str,
    session_id: str,
    questions: list[dict],
    message_id: str | None = None,
) -> None:
    """Write a pending question file."""
    pending_dir = _pending_questions_dir(source_group)

    data = {
        "request_id": request_id,
        "short_id": request_id[:8],
        "source_group": source_group,
        "chat_jid": chat_jid,
        "channel_name": channel_name,
        "session_id": session_id,
        "questions": questions,
        "message_id": message_id,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    filepath = pending_dir / f"{request_id}.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    logger.info(
        "Pending question created",
        request_id=request_id,
        short_id=request_id[:8],
        source_group=source_group,
    )


def find_pending_question(request_id: str) -> dict | None:
    """Find a pending question by full request_id across all groups."""
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"
    if not ipc_dir.exists():
        return None

    for group_dir in ipc_dir.iterdir():
        if not group_dir.is_dir() or group_dir.name == "errors":
            continue
        filepath = group_dir / "pending_questions" / f"{request_id}.json"
        if filepath.exists():
            try:
                return json.loads(filepath.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return None


def resolve_pending_question(request_id: str, source_group: str) -> None:
    """Delete a pending question file after it has been answered."""
    filepath = _pending_questions_dir(source_group) / f"{request_id}.json"
    filepath.unlink(missing_ok=True)
    logger.info("Pending question resolved", request_id=request_id)


def update_message_id(request_id: str, source_group: str, message_id: str) -> None:
    """Update the message_id field after the widget is posted to the channel."""
    filepath = _pending_questions_dir(source_group) / f"{request_id}.json"
    if not filepath.exists():
        return
    data = json.loads(filepath.read_text())
    data["message_id"] = message_id
    temp = filepath.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.rename(filepath)
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_pending_questions.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/chat/pending_questions.py tests/test_pending_questions.py
git commit -m "feat(chat): add file-based pending question state manager"
```

---

### Task 4: Host-Side IPC Handler for `ask_user:` Prefix

**Files:**
- Create: `src/pynchy/ipc/_handlers_ask_user.py`
- Modify: `src/pynchy/ipc/__init__.py` (add import to trigger registration)
- Test: `tests/test_ipc_ask_user_handler.py`

**Step 1: Write the failing test**

Test that the handler stores a pending question and calls the channel's `send_ask_user`. Check the approval gate tests at `tests/test_ipc_approval_handler.py` for the pattern to follow.

**Step 2: Implement the handler**

Key logic:
- Registered as `register_prefix("ask_user:", _handle_ask_user_request)`
- Parses `request_id`, `questions` from the task data
- Resolves `chat_jid` and channel for the source group (using `deps.workspaces()`)
- Calls `create_pending_question(...)` to store state
- Iterates `deps.channels()` to find the channel that owns this group's JID
- If channel has `send_ask_user`: calls it, stores returned `message_id`
- If channel lacks `send_ask_user`: writes error to `ipc/responses/` immediately

**Step 3: Run tests, commit**

```bash
git add src/pynchy/ipc/_handlers_ask_user.py src/pynchy/ipc/__init__.py \
       tests/test_ipc_ask_user_handler.py
git commit -m "feat(ipc): add ask_user prefix handler — routes questions to channels"
```

---

### Task 5: Slack Block Kit Widget — `send_ask_user` and Interaction Handler

**Files:**
- Modify: `src/pynchy/chat/plugins/slack.py`
- Test: `tests/test_slack_ask_user.py`

**Step 1: Write the failing test**

Test that `send_ask_user` builds correct Block Kit payload and that the
interaction handler writes the response file.

**Step 2: Implement `send_ask_user` on `SlackChannel`**

- Build Block Kit blocks:
  - `section` block with question text (markdown)
  - `actions` block with `button` elements (one per option)
  - `input` block with `plain_text_input` for free-form "Other" answer
- `block_id` encodes `request_id` for matching callbacks
- `action_id` encodes the option label
- Post via `chat_postMessage(channel=channel_id, blocks=blocks, text=fallback)`
- Return the message `ts` as `message_id`

**Step 3: Register interaction handlers in `_register_handlers`**

- `block_actions` handler for button clicks:
  - Extract `request_id` from `block_id`, `answer` from `action_id`
  - Call the answer delivery callback (see Task 6)
- `block_actions` handler for the text input submit button:
  - Extract `request_id` and free-form text value

**Step 4: Update the widget after answer**

Use `chat_update` to replace the interactive blocks with a static section:
"Answered: [selected option]"

**Step 5: Run tests, commit**

```bash
git add src/pynchy/chat/plugins/slack.py tests/test_slack_ask_user.py
git commit -m "feat(slack): add Block Kit send_ask_user and interaction handlers"
```

---

### Task 6: Answer Delivery — Wire Channel Callbacks to IPC Response

**Files:**
- Create: `src/pynchy/chat/ask_user_handler.py`
- Modify: `src/pynchy/app.py` (wire the handler)
- Test: `tests/test_ask_user_handler.py`

**Step 1: Write the failing test**

Test both paths:
- Path A: Container alive → writes to `ipc/responses/{request_id}.json`, resolves pending question
- Path B: Container dead → cold-starts container with answer context

**Step 2: Implement `ask_user_handler.py`**

```python
async def handle_ask_user_answer(
    request_id: str,
    answer: dict,
    deps: AskUserDeps,
) -> None:
    """Route a user's answer to the waiting container or cold-start if dead."""
    pending = find_pending_question(request_id)
    if pending is None:
        logger.warning("Answer for unknown question", request_id=request_id)
        return

    source_group = pending["source_group"]
    session = get_session(source_group)

    if session is not None and session.is_alive:
        # Path A: container alive — write response file
        write_ipc_response(
            response_path(source_group, request_id),
            {"result": {"answers": answer}},
        )
    else:
        # Path B: container dead — cold-start with answer context
        # Package as a message that run_agent will pick up
        answer_text = _format_answer_context(pending, answer)
        await deps.enqueue_message(pending["chat_jid"], answer_text, sender="system")

    resolve_pending_question(request_id, source_group)
```

For Path B, `_format_answer_context` formats:
```
You previously asked the user: "Which auth strategy?"
Options: 1. JWT tokens, 2. Session cookies, 3. OAuth 2.0
The user answered: "JWT tokens"
Continue from where you left off.
```

**Step 3: Wire into `app.py`**

The Slack interaction handler (from Task 5) needs a callback to invoke
`handle_ask_user_answer`. Pass it via the channel's constructor or via
a callback registered during `_register_handlers`.

Follow the pattern used by `on_message` and `on_reaction` callbacks.

**Step 4: Run tests, commit**

```bash
git add src/pynchy/chat/ask_user_handler.py src/pynchy/app.py \
       tests/test_ask_user_handler.py
git commit -m "feat(chat): wire answer delivery — live IPC response or cold-start"
```

---

### Task 7: WhatsApp Fallback — Numbered Text Options

**Files:**
- Modify: `src/pynchy/chat/plugins/whatsapp/channel.py`
- Test: `tests/test_whatsapp_ask_user.py`

**Step 1: Implement `send_ask_user` on `WhatsAppChannel`**

- Format questions as numbered text:
  ```
  The agent is asking: Which auth strategy?
  1. JWT tokens
  2. Session cookies
  3. OAuth 2.0

  Reply with a number or type your own answer.
  ```
- Store the request_id → option mapping somewhere retrievable (pending question file already has it)

**Step 2: Add answer matching in `_on_whatsapp_message`**

When a message arrives for a group with a pending question:
- Check if the message is a number matching an option
- Check if it's free-form text (treat as "Other")
- Call `handle_ask_user_answer(request_id, answer)`
- Skip normal message pipeline (don't queue as a new agent turn)

**Step 3: Run tests, commit**

```bash
git add src/pynchy/chat/plugins/whatsapp/channel.py tests/test_whatsapp_ask_user.py
git commit -m "feat(whatsapp): add numbered-text ask_user fallback"
```

---

### Task 8: Startup Sweep for Stale Pending Questions

**Files:**
- Modify: `src/pynchy/chat/pending_questions.py` (add `sweep_expired_questions`)
- Modify: `src/pynchy/app.py` (call sweep on startup, alongside approval sweep)
- Test: `tests/test_pending_questions.py` (add sweep test)

**Step 1: Add sweep function**

Follow `security/approval.py:sweep_expired_approvals()`. Auto-expire pending
questions older than 30 minutes (matching the container-side timeout). Write
error response to `ipc/responses/` for any that still have a live container.

**Step 2: Wire into startup**

In `app.py`, call `sweep_expired_questions()` alongside `sweep_expired_approvals()`.

**Step 3: Run tests, commit**

```bash
git add src/pynchy/chat/pending_questions.py src/pynchy/app.py \
       tests/test_pending_questions.py
git commit -m "feat(chat): add startup sweep for stale pending questions"
```

---

### Task 9: Integration Test — Full Round-Trip

**Files:**
- Create: `tests/test_ask_user_e2e.py`

**Step 1: Write end-to-end test**

Test the full flow with mocked channel:
1. Simulate container writing `ask_user:ask` task to `ipc/tasks/`
2. Verify pending question file created
3. Verify channel's `send_ask_user` called with correct payload
4. Simulate channel callback with answer
5. Verify response file written to `ipc/responses/`
6. Verify pending question file deleted

Follow the pattern in `tests/test_approval_e2e.py`.

**Step 2: Test late-answer path**

1. Create pending question with no live session
2. Simulate channel callback
3. Verify cold-start triggered with answer context

**Step 3: Commit**

```bash
git add tests/test_ask_user_e2e.py
git commit -m "test(e2e): add full round-trip integration tests for ask_user"
```
