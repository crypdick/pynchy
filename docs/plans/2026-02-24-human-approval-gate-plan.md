# Human Approval Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the file-backed state machine that gates high-risk service operations behind explicit human approve/deny commands.

**Architecture:** When `SecurityPolicy.evaluate_write()` returns `needs_human=True`, write a pending approval file instead of an IPC response. The container blocks naturally (it polls for a response file that doesn't exist yet). The user sends `approve <id>` or `deny <id>` in chat, which writes a decision file. The IPC watcher picks up the decision file, executes or denies the original request, and writes the IPC response file so the container unblocks.

**Tech Stack:** Python 3.12, watchdog (existing dependency), pytest, structlog

**Design doc:** `docs/plans/2026-02-24-human-approval-gate-design.md`

---

### Task 1: Approval State Manager — create and list

**Files:**
- Create: `src/pynchy/security/approval.py`
- Test: `tests/test_approval.py`

**Step 1: Write the failing tests**

```python
# tests/test_approval.py
"""Tests for the approval state manager."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import make_settings


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    """Create and return a temporary IPC directory."""
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


class TestCreatePendingApproval:
    def test_creates_pending_file(self, ipc_dir: Path, settings):
        from pynchy.security.approval import create_pending_approval

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval(
                request_id="aabb001122334455",
                tool_name="x_post",
                source_group="personal",
                chat_jid="group@g.us",
                request_data={"type": "service:x_post", "text": "hello"},
            )

        pending_dir = ipc_dir / "personal" / "pending_approvals"
        files = list(pending_dir.glob("*.json"))
        assert len(files) == 1
        assert files[0].name == "aabb001122334455.json"

        data = json.loads(files[0].read_text())
        assert data["request_id"] == "aabb001122334455"
        assert data["short_id"] == "aabb0011"
        assert data["tool_name"] == "x_post"
        assert data["source_group"] == "personal"
        assert data["chat_jid"] == "group@g.us"
        assert data["request_data"]["text"] == "hello"
        assert "timestamp" in data

    def test_uses_atomic_write(self, ipc_dir: Path, settings):
        """Pending file should be written atomically (tmp + rename)."""
        from pynchy.security.approval import create_pending_approval

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval(
                request_id="abc123",
                tool_name="test",
                source_group="grp",
                chat_jid="j@g.us",
                request_data={},
            )

        # No .tmp files left behind
        pending_dir = ipc_dir / "grp" / "pending_approvals"
        assert not list(pending_dir.glob("*.tmp"))


class TestListPendingApprovals:
    def test_lists_all_pending(self, ipc_dir: Path, settings):
        from pynchy.security.approval import create_pending_approval, list_pending_approvals

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval("req1", "tool_a", "grp1", "j1@g.us", {})
            create_pending_approval("req2", "tool_b", "grp2", "j2@g.us", {})

            result = list_pending_approvals()

        assert len(result) == 2
        tool_names = {r["tool_name"] for r in result}
        assert tool_names == {"tool_a", "tool_b"}

    def test_filters_by_group(self, ipc_dir: Path, settings):
        from pynchy.security.approval import create_pending_approval, list_pending_approvals

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval("req1", "tool_a", "grp1", "j1@g.us", {})
            create_pending_approval("req2", "tool_b", "grp2", "j2@g.us", {})

            result = list_pending_approvals(group="grp1")

        assert len(result) == 1
        assert result[0]["tool_name"] == "tool_a"

    def test_empty_when_no_pending(self, ipc_dir: Path, settings):
        from pynchy.security.approval import list_pending_approvals

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            result = list_pending_approvals()

        assert result == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_approval.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pynchy.security.approval'`

**Step 3: Write minimal implementation**

```python
# src/pynchy/security/approval.py
"""File-backed approval state manager for the human approval gate.

Manages pending approval files in ipc/{group}/pending_approvals/.
Each file represents a PENDING state in the approval state machine.
The container blocks naturally (no response file written) until
the user approves or denies via chat command.

See docs/plans/2026-02-24-human-approval-gate-design.md
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger

# Fields to omit from notification details
_INTERNAL_FIELDS = frozenset({"type", "request_id", "source_group"})

# Maximum characters for a detail value in notifications
_MAX_DETAIL_LEN = 100


def _pending_approvals_dir(source_group: str) -> Path:
    """Return the pending_approvals directory for a group, creating it if needed."""
    s = get_settings()
    d = s.data_dir / "ipc" / source_group / "pending_approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _approval_decisions_dir(source_group: str) -> Path:
    """Return the approval_decisions directory for a group, creating it if needed."""
    s = get_settings()
    d = s.data_dir / "ipc" / source_group / "approval_decisions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_pending_approval(
    request_id: str,
    tool_name: str,
    source_group: str,
    chat_jid: str,
    request_data: dict,
) -> None:
    """Write a pending approval file (PENDING state).

    The file contains everything needed to execute the request later,
    so the decision handler is self-contained.
    """
    pending_dir = _pending_approvals_dir(source_group)

    data = {
        "request_id": request_id,
        "short_id": request_id[:8],
        "tool_name": tool_name,
        "source_group": source_group,
        "chat_jid": chat_jid,
        "request_data": request_data,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    filepath = pending_dir / f"{request_id}.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    logger.info(
        "Pending approval created",
        request_id=request_id,
        short_id=request_id[:8],
        tool_name=tool_name,
        source_group=source_group,
    )


def list_pending_approvals(group: str | None = None) -> list[dict]:
    """List all pending approval files, optionally filtered by group.

    Returns a list of parsed pending approval dicts, sorted by timestamp.
    """
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"

    if not ipc_dir.exists():
        return []

    results: list[dict] = []

    groups = [group] if group else [
        f.name for f in ipc_dir.iterdir()
        if f.is_dir() and f.name != "errors"
    ]

    for grp in groups:
        pending_dir = ipc_dir / grp / "pending_approvals"
        if not pending_dir.exists():
            continue
        for filepath in pending_dir.glob("*.json"):
            try:
                data = json.loads(filepath.read_text())
                results.append(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read pending approval", path=str(filepath), err=str(exc))

    results.sort(key=lambda d: d.get("timestamp", ""))
    return results


def format_approval_notification(
    tool_name: str,
    request_data: dict,
    short_id: str,
) -> str:
    """Format a user-facing approval notification message.

    Sanitizes request data: omits internal fields, truncates long values.
    """
    details = {
        k: v for k, v in request_data.items()
        if k not in _INTERNAL_FIELDS and not k.startswith("_")
    }

    detail_parts: list[str] = []
    for key, value in details.items():
        s = str(value)
        if len(s) > _MAX_DETAIL_LEN:
            s = s[:_MAX_DETAIL_LEN] + "..."
        detail_parts.append(f"  {key}: {s}")

    details_str = "\n".join(detail_parts) if detail_parts else "  (no details)"

    return (
        f"🔐 Approval required\n"
        f"\n"
        f"Action: {tool_name}\n"
        f"Details:\n"
        f"{details_str}\n"
        f"\n"
        f"→ approve {short_id}  /  deny {short_id}"
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_approval.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add src/pynchy/security/approval.py tests/test_approval.py
git commit -m "feat(security): add approval state manager — create and list pending approvals"
```

---

### Task 2: Approval State Manager — sweep expired

**Files:**
- Modify: `src/pynchy/security/approval.py`
- Modify: `tests/test_approval.py`

**Step 1: Write the failing tests**

Append to `tests/test_approval.py`:

```python
from pynchy.ipc._handlers_service import _write_response


class TestSweepExpiredApprovals:
    async def test_expires_old_pending(self, ipc_dir: Path, settings):
        from pynchy.security.approval import create_pending_approval, sweep_expired_approvals

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval("req-old", "tool_a", "grp", "j@g.us", {})

            # Backdate the file's timestamp to 10 minutes ago
            pending_file = ipc_dir / "grp" / "pending_approvals" / "req-old.json"
            data = json.loads(pending_file.read_text())
            from datetime import timedelta
            data["timestamp"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            pending_file.write_text(json.dumps(data))

            expired = await sweep_expired_approvals()

        assert len(expired) == 1
        assert expired[0]["request_id"] == "req-old"

        # Pending file should be deleted
        assert not pending_file.exists()

        # Error response file should be written
        response_file = ipc_dir / "grp" / "responses" / "req-old.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "expired" in response["error"].lower()

    async def test_keeps_fresh_pending(self, ipc_dir: Path, settings):
        from pynchy.security.approval import create_pending_approval, sweep_expired_approvals

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval("req-fresh", "tool_b", "grp", "j@g.us", {})

            expired = await sweep_expired_approvals()

        assert len(expired) == 0

        # Pending file should still exist
        pending_file = ipc_dir / "grp" / "pending_approvals" / "req-fresh.json"
        assert pending_file.exists()

    async def test_sweep_cleans_orphaned_decisions(self, ipc_dir: Path, settings):
        """Decision files with no matching pending file should be deleted."""
        from pynchy.security.approval import sweep_expired_approvals

        # Create an orphaned decision file (no pending file exists)
        decisions_dir = ipc_dir / "grp" / "approval_decisions"
        decisions_dir.mkdir(parents=True)
        orphan = decisions_dir / "orphan-req.json"
        orphan.write_text(json.dumps({"request_id": "orphan-req", "approved": True}))

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            await sweep_expired_approvals()

        assert not orphan.exists()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_approval.py::TestSweepExpiredApprovals -v`
Expected: FAIL — `ImportError: cannot import name 'sweep_expired_approvals'`

**Step 3: Add sweep_expired_approvals to approval.py**

Add to `src/pynchy/security/approval.py`:

```python
from pynchy.security.audit import record_security_event

# How long before a pending approval is considered expired (seconds)
APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes (matches container-side timeout)


def _write_response(source_group: str, request_id: str, response: dict) -> None:
    """Write an IPC response file. Replicates _handlers_service._write_response."""
    s = get_settings()
    responses_dir = s.data_dir / "ipc" / source_group / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    filepath = responses_dir / f"{request_id}.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(response, indent=2))
    temp_path.rename(filepath)


async def sweep_expired_approvals() -> list[dict]:
    """Find and auto-deny expired pending approvals. Clean orphaned decisions.

    Returns list of expired approval dicts (for re-notification or logging).
    Called on startup (crash recovery) and optionally on a slow timer.
    """
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"
    if not ipc_dir.exists():
        return []

    now = datetime.now(UTC)
    expired: list[dict] = []

    groups = [
        f.name for f in ipc_dir.iterdir()
        if f.is_dir() and f.name != "errors"
    ]

    for grp in groups:
        pending_dir = ipc_dir / grp / "pending_approvals"
        decisions_dir = ipc_dir / grp / "approval_decisions"

        # Sweep expired pending approvals
        if pending_dir.exists():
            for filepath in list(pending_dir.glob("*.json")):
                try:
                    data = json.loads(filepath.read_text())
                    ts = datetime.fromisoformat(data["timestamp"])
                    age = (now - ts).total_seconds()

                    if age > APPROVAL_TIMEOUT_SECONDS:
                        # Auto-deny: write error response
                        _write_response(grp, data["request_id"], {
                            "error": "Approval expired (no response within timeout)",
                        })

                        await record_security_event(
                            chat_jid=data.get("chat_jid", "unknown"),
                            workspace=grp,
                            tool_name=data.get("tool_name", "unknown"),
                            decision="approval_expired",
                            request_id=data["request_id"],
                        )

                        filepath.unlink()
                        expired.append(data)

                        logger.info(
                            "Expired pending approval auto-denied",
                            request_id=data["request_id"],
                            tool_name=data.get("tool_name"),
                            age_seconds=round(age),
                        )
                except (json.JSONDecodeError, OSError, KeyError) as exc:
                    logger.warning("Failed to process pending approval", path=str(filepath), err=str(exc))

        # Clean orphaned decision files (decision exists but no matching pending)
        if decisions_dir.exists():
            pending_ids = set()
            if pending_dir.exists():
                pending_ids = {f.stem for f in pending_dir.glob("*.json")}

            for filepath in list(decisions_dir.glob("*.json")):
                if filepath.stem not in pending_ids:
                    logger.info("Removing orphaned decision file", path=str(filepath))
                    filepath.unlink(missing_ok=True)

    return expired
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_approval.py -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add src/pynchy/security/approval.py tests/test_approval.py
git commit -m "feat(security): add sweep for expired approvals and orphaned decisions"
```

---

### Task 3: Notification formatting and sanitization

**Files:**
- Modify: `tests/test_approval.py`

**Step 1: Write the failing tests**

Append to `tests/test_approval.py`:

```python
class TestFormatApprovalNotification:
    def test_basic_format(self):
        from pynchy.security.approval import format_approval_notification

        msg = format_approval_notification(
            tool_name="x_post",
            request_data={"text": "Hello world"},
            short_id="a7f3b2c1",
        )

        assert "x_post" in msg
        assert "a7f3b2c1" in msg
        assert "approve a7f3b2c1" in msg
        assert "deny a7f3b2c1" in msg
        assert "Hello world" in msg

    def test_omits_internal_fields(self):
        from pynchy.security.approval import format_approval_notification

        msg = format_approval_notification(
            tool_name="x_post",
            request_data={
                "type": "service:x_post",
                "request_id": "secret-id",
                "source_group": "grp",
                "text": "visible",
            },
            short_id="abc12345",
        )

        assert "service:x_post" not in msg
        assert "secret-id" not in msg
        assert "source_group" not in msg
        assert "visible" in msg

    def test_truncates_long_values(self):
        from pynchy.security.approval import format_approval_notification

        long_text = "x" * 200
        msg = format_approval_notification(
            tool_name="tool",
            request_data={"body": long_text},
            short_id="abc12345",
        )

        assert "..." in msg
        assert long_text not in msg

    def test_empty_request_data(self):
        from pynchy.security.approval import format_approval_notification

        msg = format_approval_notification(
            tool_name="tool",
            request_data={},
            short_id="abc12345",
        )

        assert "no details" in msg.lower()
```

**Step 2: Run tests to verify they pass** (implementation already exists from Task 1)

Run: `uv run pytest tests/test_approval.py::TestFormatApprovalNotification -v`
Expected: All 4 tests PASS (format_approval_notification was written in Task 1)

**Step 3: Commit**

```bash
git add tests/test_approval.py
git commit -m "test(security): add notification formatting tests"
```

---

### Task 4: Command matchers — is_approval_command and is_pending_query

**Files:**
- Modify: `src/pynchy/chat/commands.py`
- Create: `tests/test_approval_commands.py`

**Step 1: Write the failing tests**

```python
# tests/test_approval_commands.py
"""Tests for approval command matchers."""

from __future__ import annotations

import pytest


class TestIsApprovalCommand:
    def test_approve_with_hex_id(self):
        from pynchy.chat.commands import is_approval_command

        result = is_approval_command("approve a7f3b2c1")
        assert result == ("approve", "a7f3b2c1")

    def test_deny_with_hex_id(self):
        from pynchy.chat.commands import is_approval_command

        result = is_approval_command("deny a7f3b2c1")
        assert result == ("deny", "a7f3b2c1")

    def test_case_insensitive(self):
        from pynchy.chat.commands import is_approval_command

        result = is_approval_command("Approve A7F3B2C1")
        assert result == ("approve", "a7f3b2c1")

    def test_strips_trigger_prefix(self):
        from pynchy.chat.commands import is_approval_command

        result = is_approval_command("@pynchy approve abc12345")
        assert result == ("approve", "abc12345")

    def test_rejects_non_hex_id(self):
        from pynchy.chat.commands import is_approval_command

        assert is_approval_command("approve not-hex!") is None

    def test_rejects_wrong_verb(self):
        from pynchy.chat.commands import is_approval_command

        assert is_approval_command("accept abc12345") is None

    def test_rejects_too_many_words(self):
        from pynchy.chat.commands import is_approval_command

        assert is_approval_command("approve abc12345 extra") is None

    def test_rejects_bare_approve(self):
        from pynchy.chat.commands import is_approval_command

        assert is_approval_command("approve") is None


class TestIsPendingQuery:
    def test_bare_pending(self):
        from pynchy.chat.commands import is_pending_query

        assert is_pending_query("pending") is True

    def test_with_trigger(self):
        from pynchy.chat.commands import is_pending_query

        assert is_pending_query("@pynchy pending") is True

    def test_case_insensitive(self):
        from pynchy.chat.commands import is_pending_query

        assert is_pending_query("Pending") is True

    def test_rejects_other_text(self):
        from pynchy.chat.commands import is_pending_query

        assert is_pending_query("show pending items") is False
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_approval_commands.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_approval_command'`

**Step 3: Add matchers to commands.py**

Add to `src/pynchy/chat/commands.py` (at the end of the file):

```python
import re

# Matches 4-32 hex chars (short_id is 8, full request_id is 32)
_HEX_ID_RE = re.compile(r"^[0-9a-f]{4,32}$")


def is_approval_command(text: str) -> tuple[str, str] | None:
    """Check if text is an approve/deny command. Returns (action, short_id) or None."""
    text = _strip_trigger(text)
    words = text.strip().lower().split()
    if len(words) != 2:
        return None
    action, short_id = words
    if action not in ("approve", "deny"):
        return None
    if not _HEX_ID_RE.match(short_id):
        return None
    return (action, short_id)


def is_pending_query(text: str) -> bool:
    """Check if text is a 'pending' query command."""
    text = _strip_trigger(text)
    return text.strip().lower() == "pending"
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_approval_commands.py -v`
Expected: All 12 tests PASS

**Step 5: Commit**

```bash
git add src/pynchy/chat/commands.py tests/test_approval_commands.py
git commit -m "feat(chat): add approval command matchers — approve/deny/pending"
```

---

### Task 5: Chat pipeline integration — intercept approval commands

**Files:**
- Modify: `src/pynchy/chat/message_handler.py`
- Create: `src/pynchy/chat/approval_handler.py`
- Create: `tests/test_approval_handler.py`

**Step 1: Write the failing tests**

```python
# tests/test_approval_handler.py
"""Tests for approval command handling in the chat pipeline."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from conftest import make_settings


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


class FakeDeps:
    """Minimal MessageHandlerDeps for testing approval handling."""

    def __init__(self):
        self.broadcast_messages: list[tuple[str, str]] = []

    async def broadcast_to_channels(self, chat_jid: str, text: str, **kwargs) -> None:
        self.broadcast_messages.append((chat_jid, text))

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        self.broadcast_messages.append((chat_jid, text))


class TestHandleApprovalCommand:
    async def test_writes_decision_file_on_approve(self, ipc_dir: Path, settings):
        from pynchy.chat.approval_handler import handle_approval_command
        from pynchy.security.approval import create_pending_approval

        with patch("pynchy.security.approval.get_settings", return_value=settings), \
             patch("pynchy.chat.approval_handler.get_settings", return_value=settings):
            create_pending_approval("aabb001122334455", "x_post", "grp", "j@g.us", {"text": "hi"})
            deps = FakeDeps()
            await handle_approval_command(deps, "j@g.us", "approve", "aabb0011", "testuser")

        decisions_dir = ipc_dir / "grp" / "approval_decisions"
        files = list(decisions_dir.glob("*.json"))
        assert len(files) == 1

        data = json.loads(files[0].read_text())
        assert data["approved"] is True
        assert data["decided_by"] == "testuser"
        assert data["request_id"] == "aabb001122334455"

    async def test_writes_decision_file_on_deny(self, ipc_dir: Path, settings):
        from pynchy.chat.approval_handler import handle_approval_command
        from pynchy.security.approval import create_pending_approval

        with patch("pynchy.security.approval.get_settings", return_value=settings), \
             patch("pynchy.chat.approval_handler.get_settings", return_value=settings):
            create_pending_approval("aabb001122334455", "x_post", "grp", "j@g.us", {"text": "hi"})

            deps = FakeDeps()
            await handle_approval_command(deps, "j@g.us", "deny", "aabb0011", "testuser")

        decisions_dir = ipc_dir / "grp" / "approval_decisions"
        data = json.loads(list(decisions_dir.glob("*.json"))[0].read_text())
        assert data["approved"] is False

    async def test_unknown_id_sends_error(self, ipc_dir: Path, settings):
        from pynchy.chat.approval_handler import handle_approval_command

        with patch("pynchy.chat.approval_handler.get_settings", return_value=settings):
            deps = FakeDeps()
            await handle_approval_command(deps, "j@g.us", "approve", "nonexist", "testuser")

        assert len(deps.broadcast_messages) == 1
        assert "no pending" in deps.broadcast_messages[0][1].lower()


class TestHandlePendingQuery:
    async def test_lists_pending_approvals(self, ipc_dir: Path, settings):
        from pynchy.chat.approval_handler import handle_pending_query
        from pynchy.security.approval import create_pending_approval

        with patch("pynchy.security.approval.get_settings", return_value=settings), \
             patch("pynchy.chat.approval_handler.get_settings", return_value=settings):
            create_pending_approval("req1", "x_post", "grp", "j@g.us", {})
            create_pending_approval("req2", "send_email", "grp", "j@g.us", {})

            deps = FakeDeps()
            await handle_pending_query(deps, "j@g.us")

        assert len(deps.broadcast_messages) == 1
        msg = deps.broadcast_messages[0][1]
        assert "x_post" in msg
        assert "send_email" in msg

    async def test_no_pending_shows_message(self, ipc_dir: Path, settings):
        from pynchy.chat.approval_handler import handle_pending_query

        with patch("pynchy.chat.approval_handler.get_settings", return_value=settings):
            deps = FakeDeps()
            await handle_pending_query(deps, "j@g.us")

        assert len(deps.broadcast_messages) == 1
        assert "no pending" in deps.broadcast_messages[0][1].lower()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_approval_handler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pynchy.chat.approval_handler'`

**Step 3: Write the approval handler**

```python
# src/pynchy/chat/approval_handler.py
"""Approval command handlers for the chat pipeline.

Handles 'approve <id>', 'deny <id>', and 'pending' commands by
writing decision files that the IPC watcher picks up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.security.approval import (
    _approval_decisions_dir,
    list_pending_approvals,
)

if TYPE_CHECKING:
    pass


class ApprovalDeps(Protocol):
    """Minimal deps needed by approval handlers."""

    async def broadcast_to_channels(
        self, chat_jid: str, text: str, **kwargs
    ) -> None: ...


def _find_pending_by_short_id(short_id: str) -> dict | None:
    """Find a pending approval file matching the given short ID prefix."""
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"
    if not ipc_dir.exists():
        return None

    for group_dir in ipc_dir.iterdir():
        if not group_dir.is_dir() or group_dir.name == "errors":
            continue
        pending_dir = group_dir / "pending_approvals"
        if not pending_dir.exists():
            continue
        for filepath in pending_dir.glob(f"{short_id}*.json"):
            try:
                return json.loads(filepath.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return None


async def handle_approval_command(
    deps: ApprovalDeps,
    chat_jid: str,
    action: str,
    short_id: str,
    sender: str,
) -> None:
    """Process an approve/deny command by writing a decision file."""
    pending = _find_pending_by_short_id(short_id)

    if pending is None:
        await deps.broadcast_to_channels(
            chat_jid,
            f"No pending approval found for ID: {short_id}",
        )
        return

    request_id = pending["request_id"]
    source_group = pending["source_group"]
    approved = action == "approve"

    decisions_dir = _approval_decisions_dir(source_group)
    decision_data = {
        "request_id": request_id,
        "approved": approved,
        "decided_by": sender,
        "decided_at": datetime.now(UTC).isoformat(),
    }

    filepath = decisions_dir / f"{request_id}.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(decision_data, indent=2))
    temp_path.rename(filepath)

    verb = "Approved" if approved else "Denied"
    await deps.broadcast_to_channels(
        chat_jid,
        f"✅ {verb}: {pending['tool_name']} ({short_id})",
    )

    logger.info(
        "Approval decision written",
        request_id=request_id,
        action=action,
        decided_by=sender,
    )


async def handle_pending_query(deps: ApprovalDeps, chat_jid: str) -> None:
    """List all pending approval requests."""
    pending = list_pending_approvals()

    if not pending:
        await deps.broadcast_to_channels(chat_jid, "No pending approvals.")
        return

    lines = ["Pending approvals:\n"]
    for p in pending:
        lines.append(
            f"  • {p['tool_name']} ({p['short_id']}) — {p.get('source_group', '?')}"
        )

    await deps.broadcast_to_channels(chat_jid, "\n".join(lines))
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_approval_handler.py -v`
Expected: All 5 tests PASS

**Step 5: Wire into intercept_special_command**

Modify `src/pynchy/chat/message_handler.py`. Add imports at the top (after existing imports):

```python
from pynchy.chat.commands import is_approval_command, is_pending_query
from pynchy.chat.approval_handler import handle_approval_command, handle_pending_query
```

In `intercept_special_command()`, add **before** the `if content.startswith("!"):` block (before line 133):

```python
    approval = is_approval_command(content)
    if approval:
        action, short_id = approval
        await handle_approval_command(deps, chat_jid, action, short_id, message.sender_name)
        deps.last_agent_timestamp[chat_jid] = message.timestamp
        await deps.save_state()
        return True

    if is_pending_query(content):
        await handle_pending_query(deps, chat_jid)
        deps.last_agent_timestamp[chat_jid] = message.timestamp
        await deps.save_state()
        return True
```

**Step 6: Run all tests**

Run: `uv run pytest tests/test_approval_handler.py tests/test_approval_commands.py tests/test_approval.py -v`
Expected: All tests PASS

**Step 7: Commit**

```bash
git add src/pynchy/chat/approval_handler.py src/pynchy/chat/message_handler.py tests/test_approval_handler.py
git commit -m "feat(chat): wire approval commands into message pipeline"
```

---

### Task 6: Decision handler — IPC watcher extension

**Files:**
- Create: `src/pynchy/ipc/_handlers_approval.py`
- Modify: `src/pynchy/ipc/_watcher.py`
- Create: `tests/test_ipc_approval_handler.py`

**Step 1: Write the failing tests**

```python
# tests/test_ipc_approval_handler.py
"""Tests for the IPC approval decision handler."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from conftest import make_settings
from pynchy.db import _init_test_database


@pytest.fixture
async def _setup_db():
    await _init_test_database()


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


def _write_pending(ipc_dir: Path, group: str, request_id: str, tool_name: str, request_data: dict) -> Path:
    """Helper to write a pending approval file."""
    pending_dir = ipc_dir / group / "pending_approvals"
    pending_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "request_id": request_id,
        "short_id": request_id[:8],
        "tool_name": tool_name,
        "source_group": group,
        "chat_jid": "j@g.us",
        "request_data": {"type": f"service:{tool_name}", "request_id": request_id, **request_data},
        "timestamp": "2026-02-24T12:00:00+00:00",
    }
    filepath = pending_dir / f"{request_id}.json"
    filepath.write_text(json.dumps(data))
    return filepath


def _write_decision(ipc_dir: Path, group: str, request_id: str, approved: bool) -> Path:
    """Helper to write a decision file."""
    decisions_dir = ipc_dir / group / "approval_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "request_id": request_id,
        "approved": approved,
        "decided_by": "testuser",
        "decided_at": "2026-02-24T12:01:00+00:00",
    }
    filepath = decisions_dir / f"{request_id}.json"
    filepath.write_text(json.dumps(data))
    return filepath


class TestProcessApprovalDecision:
    async def test_approved_executes_and_writes_response(self, _setup_db, ipc_dir: Path, settings):
        from pynchy.ipc._handlers_approval import process_approval_decision

        _write_pending(ipc_dir, "grp", "req123", "my_tool", {"arg": "val"})
        decision_file = _write_decision(ipc_dir, "grp", "req123", approved=True)

        mock_handler = AsyncMock(return_value={"result": {"status": "posted"}})

        with patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings), \
             patch("pynchy.ipc._handlers_approval._get_plugin_handlers", return_value={"my_tool": mock_handler}):
            await process_approval_decision(decision_file, "grp")

        # Handler was called with original request data
        mock_handler.assert_awaited_once()
        call_data = mock_handler.call_args[0][0]
        assert call_data["arg"] == "val"

        # Response file written
        response_file = ipc_dir / "grp" / "responses" / "req123.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert response["result"]["status"] == "posted"

        # Pending and decision files cleaned up
        assert not (ipc_dir / "grp" / "pending_approvals" / "req123.json").exists()
        assert not decision_file.exists()

    async def test_denied_writes_error_response(self, _setup_db, ipc_dir: Path, settings):
        from pynchy.ipc._handlers_approval import process_approval_decision

        _write_pending(ipc_dir, "grp", "req456", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "req456", approved=False)

        with patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings):
            await process_approval_decision(decision_file, "grp")

        response_file = ipc_dir / "grp" / "responses" / "req456.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "denied" in response["error"].lower()

        # Cleaned up
        assert not (ipc_dir / "grp" / "pending_approvals" / "req456.json").exists()
        assert not decision_file.exists()

    async def test_missing_pending_cleans_decision(self, _setup_db, ipc_dir: Path, settings):
        """Decision with no matching pending file should be cleaned up."""
        from pynchy.ipc._handlers_approval import process_approval_decision

        decision_file = _write_decision(ipc_dir, "grp", "orphan", approved=True)

        with patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings):
            await process_approval_decision(decision_file, "grp")

        assert not decision_file.exists()

    async def test_unknown_tool_writes_error(self, _setup_db, ipc_dir: Path, settings):
        """Approved request for unknown tool should write error response."""
        from pynchy.ipc._handlers_approval import process_approval_decision

        _write_pending(ipc_dir, "grp", "req789", "nonexistent_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "req789", approved=True)

        with patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings), \
             patch("pynchy.ipc._handlers_approval._get_plugin_handlers", return_value={}):
            await process_approval_decision(decision_file, "grp")

        response_file = ipc_dir / "grp" / "responses" / "req789.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ipc_approval_handler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pynchy.ipc._handlers_approval'`

**Step 3: Write the decision handler**

```python
# src/pynchy/ipc/_handlers_approval.py
"""IPC handler for approval decision files.

When a decision file appears in approval_decisions/, this handler:
- Reads the decision and corresponding pending approval
- Executes the original request (if approved) or writes error (if denied)
- Writes the IPC response file so the container unblocks
- Cleans up pending and decision files
"""

from __future__ import annotations

import json
from pathlib import Path

from pynchy.config import get_settings
from pynchy.ipc._handlers_service import _get_plugin_handlers
from pynchy.logger import logger
from pynchy.security.audit import record_security_event


def _write_response(source_group: str, request_id: str, response: dict) -> None:
    """Write an IPC response file (same as _handlers_service._write_response)."""
    s = get_settings()
    responses_dir = s.data_dir / "ipc" / source_group / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    filepath = responses_dir / f"{request_id}.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(response, indent=2))
    temp_path.rename(filepath)


async def process_approval_decision(decision_file: Path, source_group: str) -> None:
    """Process an approval decision file — execute or deny the original request."""
    try:
        decision = json.loads(decision_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read decision file", path=str(decision_file), err=str(exc))
        decision_file.unlink(missing_ok=True)
        return

    request_id = decision.get("request_id")
    if not request_id:
        logger.warning("Decision file missing request_id", path=str(decision_file))
        decision_file.unlink(missing_ok=True)
        return

    # Find the corresponding pending approval
    s = get_settings()
    pending_file = s.data_dir / "ipc" / source_group / "pending_approvals" / f"{request_id}.json"

    if not pending_file.exists():
        logger.warning("No pending approval for decision", request_id=request_id)
        decision_file.unlink(missing_ok=True)
        return

    try:
        pending = json.loads(pending_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read pending file", path=str(pending_file), err=str(exc))
        decision_file.unlink(missing_ok=True)
        pending_file.unlink(missing_ok=True)
        return

    tool_name = pending.get("tool_name", "unknown")
    chat_jid = pending.get("chat_jid", "unknown")
    request_data = pending.get("request_data", {})
    approved = decision.get("approved", False)

    if approved:
        # Execute the original request
        handlers = _get_plugin_handlers()
        handler = handlers.get(tool_name)

        if handler is None:
            logger.warning("Approved tool no longer available", tool_name=tool_name)
            _write_response(source_group, request_id, {
                "error": f"Approved but tool '{tool_name}' is no longer available",
            })
        else:
            try:
                request_data["source_group"] = source_group
                response = await handler(request_data)
                _write_response(source_group, request_id, response)
                logger.info("Approved request executed", request_id=request_id, tool_name=tool_name)
            except Exception as exc:
                logger.error("Approved request failed", request_id=request_id, err=str(exc))
                _write_response(source_group, request_id, {"error": f"Execution failed: {exc}"})

        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="approved_by_user",
            request_id=request_id,
        )
    else:
        _write_response(source_group, request_id, {"error": "Denied by user"})
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="denied_by_user",
            request_id=request_id,
        )
        logger.info("Denied request", request_id=request_id, tool_name=tool_name)

    # Clean up files
    pending_file.unlink(missing_ok=True)
    decision_file.unlink(missing_ok=True)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ipc_approval_handler.py -v`
Expected: All 4 tests PASS

**Step 5: Wire into the IPC watcher**

Modify `src/pynchy/ipc/_watcher.py`:

1. Add `"approval_decisions"` to the allowed subdirs in `_enqueue_if_ipc` (line 229):

Change:
```python
if len(parts) == 3 and parts[1] in ("messages", "tasks"):
```
To:
```python
if len(parts) == 3 and parts[1] in ("messages", "tasks", "approval_decisions"):
```

2. Add the decision processing branch in `_process_queue` (after `elif subdir == "tasks":` block, around line 269):

```python
            elif subdir == "approval_decisions":
                from pynchy.ipc._handlers_approval import process_approval_decision
                await process_approval_decision(file_path, source_group)
```

3. Extend `_sweep_directory` to call `sweep_expired_approvals` after the existing sweep (after line 201, before `return processed`):

```python
    # Sweep expired approvals (crash recovery)
    from pynchy.security.approval import sweep_expired_approvals
    expired = await sweep_expired_approvals()
    if expired:
        logger.info("Expired approvals auto-denied during sweep", count=len(expired))
```

**Step 6: Run all tests**

Run: `uv run pytest tests/test_ipc_approval_handler.py tests/test_ipc_watcher_v2.py -v`
Expected: All tests PASS

**Step 7: Commit**

```bash
git add src/pynchy/ipc/_handlers_approval.py src/pynchy/ipc/_watcher.py tests/test_ipc_approval_handler.py
git commit -m "feat(ipc): add approval decision handler and wire into watcher"
```

---

### Task 7: Replace the service handler stub

**Files:**
- Modify: `src/pynchy/ipc/_handlers_service.py`
- Modify: `tests/test_ipc_service_handler.py`

**Step 1: Update the existing test expectation**

In `tests/test_ipc_service_handler.py`, the test `test_dangerous_writes_requires_human` (line 151) currently asserts an error response is written. After this change, **no response file should be written** (the container blocks). Update:

```python
@pytest.mark.asyncio
async def test_dangerous_writes_creates_pending_approval(tmp_path):
    """Test that dangerous_writes=True creates a pending approval file (no response)."""
    fake_pm = _make_fake_plugin_manager("sensitive_tool")
    settings = _make_settings(
        ws_security=WorkspaceSecurityTomlConfig(
            services={
                "sensitive_tool": ServiceTrustTomlConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=True,
                ),
            },
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})
    # Add broadcast_to_channels to FakeDeps
    deps.broadcast_messages = []
    deps.broadcast_to_channels = AsyncMock()

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = _make_request("sensitive_tool", item_id="123")
        await _handle_service_request(data, "test-ws", False, deps)

    # No response file written (container blocks)
    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert not response_file.exists()

    # Pending approval file created instead
    pending_dir = tmp_path / "ipc" / "test-ws" / "pending_approvals"
    files = list(pending_dir.glob("*.json"))
    assert len(files) == 1

    # Notification broadcast
    deps.broadcast_to_channels.assert_awaited_once()
```

Similarly update `test_fallback_security_for_unconfigured_workspace`.

**Step 2: Run test to verify it fails** (old assertion no longer correct after code change)

**Step 3: Replace the stub in _handlers_service.py**

In `src/pynchy/ipc/_handlers_service.py`, add import at the top:

```python
from pynchy.security.approval import create_pending_approval, format_approval_notification
```

Replace the `needs_human` block (lines 176-198):

```python
    if decision.needs_human:
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="approval_requested",
            corruption_tainted=policy.corruption_tainted,
            secret_tainted=policy.secret_tainted,
            reason=decision.reason,
            request_id=request_id,
        )
        create_pending_approval(
            request_id=request_id,
            tool_name=tool_name,
            source_group=source_group,
            chat_jid=chat_jid,
            request_data=data,
        )
        await deps.broadcast_to_channels(
            chat_jid,
            format_approval_notification(
                tool_name=tool_name,
                request_data=data,
                short_id=request_id[:8],
            ),
        )
        logger.info(
            "Service request pending human approval",
            tool_name=tool_name,
            source_group=source_group,
            short_id=request_id[:8],
        )
        # No response file written — container blocks until user decides
        return
```

**Step 4: Run all tests**

Run: `uv run pytest tests/test_ipc_service_handler.py tests/test_approval.py tests/test_approval_commands.py tests/test_approval_handler.py tests/test_ipc_approval_handler.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/pynchy/ipc/_handlers_service.py tests/test_ipc_service_handler.py
git commit -m "feat(security): replace approval stub with file-backed state machine"
```

---

### Task 8: Export and update __init__.py

**Files:**
- Modify: `src/pynchy/security/__init__.py`

**Step 1: Add approval exports**

```python
from pynchy.security.approval import (
    create_pending_approval,
    format_approval_notification,
    list_pending_approvals,
    sweep_expired_approvals,
)
```

Add them to `__all__`.

**Step 2: Run the full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests PASS, no regressions

**Step 3: Commit**

```bash
git add src/pynchy/security/__init__.py
git commit -m "feat(security): export approval functions from security package"
```

---

### Task 9: End-to-end integration test

**Files:**
- Create: `tests/test_approval_e2e.py`

**Step 1: Write the integration test**

```python
# tests/test_approval_e2e.py
"""End-to-end test for the human approval gate.

Tests the full flow: service request → pending approval → user approve →
decision handler → response file → container unblocks.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from conftest import make_settings
from pynchy.config_models import ServiceTrustTomlConfig, WorkspaceConfig, WorkspaceSecurityTomlConfig
from pynchy.db import _init_test_database
from pynchy.ipc._handlers_approval import process_approval_decision
from pynchy.ipc._handlers_service import _handle_service_request, clear_plugin_handler_cache
from pynchy.types import WorkspaceProfile


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()
    clear_plugin_handler_cache()


TEST_GROUP = WorkspaceProfile(
    jid="test@g.us", name="Test", folder="test-ws",
    trigger="@Pynchy", added_at="2024-01-01",
)


class FakeDeps:
    def __init__(self):
        self._groups = {"test@g.us": TEST_GROUP}
        self.broadcast_calls: list[tuple[str, str]] = []

    def workspaces(self):
        return self._groups

    async def broadcast_to_channels(self, jid, text):
        self.broadcast_calls.append((jid, text))


@pytest.mark.asyncio
async def test_full_approve_flow(tmp_path: Path):
    """Full flow: dangerous_writes → pending → approve → execute → response."""
    mock_handler = AsyncMock(return_value={"result": {"posted": True}})

    from unittest.mock import MagicMock
    fake_pm = MagicMock()
    fake_pm.hook.pynchy_service_handler.return_value = [
        {"tools": {"x_post": mock_handler}},
    ]

    class FakeSettings:
        def __init__(self):
            self.data_dir = tmp_path
            self.workspaces = {
                "test-ws": WorkspaceConfig(
                    name="test",
                    security=WorkspaceSecurityTomlConfig(
                        services={
                            "x_post": ServiceTrustTomlConfig(
                                public_source=False, secret_data=False,
                                public_sink=False, dangerous_writes=True,
                            ),
                        },
                    ),
                ),
            }
            self.services = {}

    settings = FakeSettings()
    deps = FakeDeps()

    # Phase 1: Service request triggers pending approval
    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch("pynchy.security.approval.get_settings", return_value=settings),
    ):
        data = {
            "type": "service:x_post",
            "request_id": "req1a2b3c4d5e6f",
            "text": "Hello world",
        }
        await _handle_service_request(data, "test-ws", False, deps)

    # No response file yet (container is blocking)
    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "req1a2b3c4d5e6f.json"
    assert not response_file.exists()

    # Pending file exists
    pending_file = tmp_path / "ipc" / "test-ws" / "pending_approvals" / "req1a2b3c4d5e6f.json"
    assert pending_file.exists()

    # Notification was broadcast
    assert len(deps.broadcast_calls) == 1
    assert "approve" in deps.broadcast_calls[0][1].lower()

    # Phase 2: User approves → decision file written
    decisions_dir = tmp_path / "ipc" / "test-ws" / "approval_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    decision_file = decisions_dir / "req1a2b3c4d5e6f.json"
    decision_file.write_text(json.dumps({
        "request_id": "req1a2b3c4d5e6f",
        "approved": True,
        "decided_by": "ricardo",
        "decided_at": "2026-02-24T12:01:00+00:00",
    }))

    # Phase 3: Decision handler picks it up and executes
    with (
        patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_approval._get_plugin_handlers", return_value={"x_post": mock_handler}),
    ):
        await process_approval_decision(decision_file, "test-ws")

    # Response file now exists (container unblocks)
    assert response_file.exists()
    response = json.loads(response_file.read_text())
    assert response["result"]["posted"] is True

    # Pending and decision files cleaned up
    assert not pending_file.exists()
    assert not decision_file.exists()

    # Handler was called with original request data
    mock_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_deny_flow(tmp_path: Path):
    """Full flow: dangerous_writes → pending → deny → error response."""
    from unittest.mock import MagicMock
    fake_pm = MagicMock()
    fake_pm.hook.pynchy_service_handler.return_value = [
        {"tools": {"x_post": AsyncMock()}},
    ]

    class FakeSettings:
        def __init__(self):
            self.data_dir = tmp_path
            self.workspaces = {
                "test-ws": WorkspaceConfig(
                    name="test",
                    security=WorkspaceSecurityTomlConfig(
                        services={
                            "x_post": ServiceTrustTomlConfig(
                                public_source=False, secret_data=False,
                                public_sink=False, dangerous_writes=True,
                            ),
                        },
                    ),
                ),
            }
            self.services = {}

    settings = FakeSettings()
    deps = FakeDeps()

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch("pynchy.security.approval.get_settings", return_value=settings),
    ):
        data = {"type": "service:x_post", "request_id": "deny123", "text": "test"}
        await _handle_service_request(data, "test-ws", False, deps)

    # Write deny decision
    decisions_dir = tmp_path / "ipc" / "test-ws" / "approval_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    decision_file = decisions_dir / "deny123.json"
    decision_file.write_text(json.dumps({
        "request_id": "deny123", "approved": False,
        "decided_by": "ricardo", "decided_at": "2026-02-24T12:01:00+00:00",
    }))

    with patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings):
        await process_approval_decision(decision_file, "test-ws")

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "deny123.json"
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "denied" in response["error"].lower()
```

**Step 2: Run to verify**

Run: `uv run pytest tests/test_approval_e2e.py -v`
Expected: All 2 tests PASS

**Step 3: Run full test suite for regressions**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests PASS

**Step 4: Commit**

```bash
git add tests/test_approval_e2e.py
git commit -m "test(security): add end-to-end integration tests for approval gate"
```

---

### Task 10: Final — lint, docs update, and move plan to completed

**Step 1: Run linter**

Run: `uvx ruff check src/pynchy/security/approval.py src/pynchy/chat/approval_handler.py src/pynchy/ipc/_handlers_approval.py src/pynchy/chat/commands.py src/pynchy/chat/message_handler.py src/pynchy/ipc/_watcher.py`
Expected: No errors

**Step 2: Run formatter**

Run: `uvx ruff format src/pynchy/security/approval.py src/pynchy/chat/approval_handler.py src/pynchy/ipc/_handlers_approval.py`
Expected: Formatted

**Step 3: Update security architecture docs**

Modify `docs/architecture/security.md` to add a section about the approval gate:

```markdown
### Human Approval Gate

When the policy middleware determines a service write requires human approval
(`needs_human=True`), the system uses a file-backed state machine:

1. A pending approval file is written and the user is notified via chat
2. The container blocks naturally (waiting for its response file)
3. The user sends `approve <id>` or `deny <id>`
4. The decision handler executes or denies the request and writes the response

Commands: `approve <id>`, `deny <id>`, `pending`

See `docs/plans/2026-02-24-human-approval-gate-design.md` for full design.
```

**Step 4: Move backlog item to completed**

```bash
git mv backlog/2-planning/security-hardening-6-approval.md backlog/5-completed/security-hardening-6-approval.md
```

Update `backlog/TODO.md`: move the Step 6 line from "2 - Planning" to remove it (completed items aren't tracked in TODO.md per the instructions at the top of that file).

**Step 5: Final commit**

```bash
git add -A
git commit -m "docs(security): update architecture docs, complete approval gate backlog item"
```
