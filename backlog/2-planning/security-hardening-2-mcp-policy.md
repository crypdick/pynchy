# Security Hardening: Step 2 - MCP Tools & Basic Policy

## Overview

Implement new MCP tools for external services (email, calendar, passwords) and add basic policy checking middleware to gate tool execution based on workspace security profiles.

## Scope

This step extends the container's MCP server with new IPC tools and adds the policy enforcement layer that evaluates tool calls against workspace security profiles. Includes rate limiting, audit logging (via existing `messages` table), and non-retryable denial classification. Does NOT implement actual service integrations yet - tools will write IPC requests that later steps will process.

## Dependencies

- ✅ Step 1: Workspace Security Profiles (must be complete)
- ✅ IPC MCP system (already implemented)

## Implementation

### 1. Extend IPC MCP Tools

**File:** `src/pynchy/agent_runner/ipc_mcp.py`

Add new MCP tools that write IPC requests:

```python
# Email tools
@server.call_tool()
async def read_email(arguments: dict) -> list[TextContent]:
    """Read emails matching filter criteria."""
    request = {
        "type": "read_email",
        "folder": arguments.get("folder", "INBOX"),
        "limit": arguments.get("limit", 10),
        "unread_only": arguments.get("unread_only", False),
    }
    return await write_ipc_request("read_email", request)


@server.call_tool()
async def send_email(arguments: dict) -> list[TextContent]:
    """Send an email (requires approval)."""
    request = {
        "type": "send_email",
        "to": arguments["to"],
        "subject": arguments["subject"],
        "body": arguments["body"],
        "cc": arguments.get("cc"),
        "bcc": arguments.get("bcc"),
    }
    return await write_ipc_request("send_email", request)


# Calendar tools
@server.call_tool()
async def list_calendar(arguments: dict) -> list[TextContent]:
    """List calendar events."""
    request = {
        "type": "list_calendar",
        "start_date": arguments.get("start_date"),
        "end_date": arguments.get("end_date"),
        "calendar": arguments.get("calendar", "primary"),
    }
    return await write_ipc_request("list_calendar", request)


@server.call_tool()
async def create_event(arguments: dict) -> list[TextContent]:
    """Create a calendar event."""
    request = {
        "type": "create_event",
        "title": arguments["title"],
        "start": arguments["start"],
        "end": arguments["end"],
        "description": arguments.get("description"),
        "location": arguments.get("location"),
        "calendar": arguments.get("calendar", "primary"),
    }
    return await write_ipc_request("create_event", request)


@server.call_tool()
async def delete_event(arguments: dict) -> list[TextContent]:
    """Delete a calendar event (requires approval)."""
    request = {
        "type": "delete_event",
        "event_id": arguments["event_id"],
        "calendar": arguments.get("calendar", "primary"),
    }
    return await write_ipc_request("delete_event", request)


# Password manager tools
@server.call_tool()
async def search_passwords(arguments: dict) -> list[TextContent]:
    """Search password vault (returns metadata only, not passwords)."""
    request = {
        "type": "search_passwords",
        "query": arguments["query"],
    }
    return await write_ipc_request("search_passwords", request)


@server.call_tool()
async def get_password(arguments: dict) -> list[TextContent]:
    """Get password from vault (requires approval)."""
    request = {
        "type": "get_password",
        "item_id": arguments["item_id"],
        "field": arguments.get("field", "password"),
    }
    return await write_ipc_request("get_password", request)
```

Helper function (already exists, may need updates):

```python
async def write_ipc_request(tool_name: str, request: dict) -> list[TextContent]:
    """Write IPC request and wait for response.

    This will be intercepted by the policy middleware in the host.
    """
    # Generate unique request ID
    request_id = str(uuid.uuid4())
    request["request_id"] = request_id

    # Write to IPC output
    output_dir = Path("/workspace/ipc/output")
    output_file = output_dir / f"{tool_name}_{request_id}.json"

    with open(output_file, "w") as f:
        json.dump(request, f)

    # Wait for response (host will create response file)
    response_file = Path("/workspace/ipc/input") / f"{request_id}_response.json"

    # Poll for response with timeout
    timeout = 300  # 5 minutes for human approval
    start = time.time()

    while time.time() - start < timeout:
        if response_file.exists():
            with open(response_file) as f:
                response = json.load(f)

            response_file.unlink()  # Clean up

            if response.get("error"):
                return [TextContent(type="text", text=f"Error: {response['error']}")]

            return [TextContent(type="text", text=json.dumps(response["result"], indent=2))]

        await asyncio.sleep(0.5)

    return [TextContent(type="text", text="Error: Request timed out")]
```

### 2. Create Policy Middleware (with Rate Limiting)

**File:** `src/pynchy/policy/middleware.py` (new file)

```python
"""Policy enforcement middleware for IPC requests."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from pynchy.types.security import RateLimitConfig, RiskTier, WorkspaceSecurityProfile

logger = logging.getLogger(__name__)


class PolicyDeniedError(Exception):
    """Raised when policy denies a request. Non-retryable."""

    pass


class PolicyDecision:
    """Result of policy evaluation."""

    def __init__(
        self,
        allowed: bool,
        reason: str | None = None,
        requires_approval: bool = False,
    ):
        self.allowed = allowed
        self.reason = reason
        self.requires_approval = requires_approval


class ActionTracker:
    """Sliding window rate limiter for tool calls."""

    def __init__(self, rate_limits: RateLimitConfig):
        self.rate_limits = rate_limits
        self._timestamps: list[float] = []  # Global call timestamps
        self._per_tool: dict[str, list[float]] = defaultdict(list)  # Per-tool timestamps
        self._window_seconds = 3600  # 1 hour

    def _prune(self, timestamps: list[float], now: float) -> list[float]:
        """Remove timestamps older than the sliding window."""
        cutoff = now - self._window_seconds
        return [t for t in timestamps if t > cutoff]

    def check_and_record(self, tool_name: str) -> tuple[bool, str | None]:
        """Check rate limit and record the call if allowed.

        Returns:
            (allowed, reason) — reason is None if allowed, error string if denied
        """
        now = time.monotonic()

        # Prune old timestamps
        self._timestamps = self._prune(self._timestamps, now)
        self._per_tool[tool_name] = self._prune(self._per_tool[tool_name], now)

        # Check global limit
        if len(self._timestamps) >= self.rate_limits["max_calls_per_hour"]:
            return False, (
                f"Global rate limit exceeded: {self.rate_limits['max_calls_per_hour']} "
                f"calls/hour"
            )

        # Check per-tool override
        per_tool_limit = self.rate_limits.get("per_tool_overrides", {}).get(tool_name)
        if per_tool_limit and len(self._per_tool[tool_name]) >= per_tool_limit:
            return False, (
                f"Per-tool rate limit exceeded for {tool_name}: {per_tool_limit} calls/hour"
            )

        # Record the call
        self._timestamps.append(now)
        self._per_tool[tool_name].append(now)
        return True, None


class PolicyMiddleware:
    """Evaluates IPC requests against workspace security profile."""

    def __init__(self, security_profile: WorkspaceSecurityProfile):
        self.security_profile = security_profile
        self.tracker: ActionTracker | None = None

        rate_limits = security_profile.get("rate_limits")
        if rate_limits:
            self.tracker = ActionTracker(rate_limits)

    def evaluate(self, tool_name: str, request: dict) -> PolicyDecision:
        """Evaluate whether tool call should be allowed.

        Checks rate limits first, then tier-based policy.

        Args:
            tool_name: Name of the MCP tool being called
            request: The IPC request payload

        Returns:
            PolicyDecision with allowed/denied status and reason
        """
        # Check rate limits first (applies to ALL tiers, even read-only)
        if self.tracker:
            allowed, reason = self.tracker.check_and_record(tool_name)
            if not allowed:
                return PolicyDecision(allowed=False, reason=reason)

        # Check if tool is in profile
        if tool_name in self.security_profile["tools"]:
            tool_profile = self.security_profile["tools"][tool_name]

            # Check if tool is enabled
            if not tool_profile["enabled"]:
                return PolicyDecision(
                    allowed=False, reason=f"Tool {tool_name} is disabled in this workspace"
                )

            tier = tool_profile["tier"]
        else:
            # Tool not in profile - use default
            if not self.security_profile["allow_unknown_tools"]:
                return PolicyDecision(
                    allowed=False,
                    reason=f"Tool {tool_name} not in security profile and unknown tools are not allowed",
                )

            tier = self.security_profile["default_tier"]

        # Evaluate based on tier
        if tier == RiskTier.READ_ONLY:
            # Auto-approved
            return PolicyDecision(allowed=True, reason="Read-only tool, auto-approved")

        elif tier == RiskTier.WRITE:
            # Apply rules engine (for now, just auto-approve - rules come later)
            return self._apply_rules(tool_name, request)

        elif tier == RiskTier.EXTERNAL:
            # Requires human approval
            return PolicyDecision(
                allowed=False,  # Will be approved later by human
                reason="External/destructive tool, requires human approval",
                requires_approval=True,
            )

        return PolicyDecision(allowed=False, reason="Unknown tier")

    def _apply_rules(self, tool_name: str, request: dict) -> PolicyDecision:
        """Apply deterministic rules for write-tier tools.

        For now, auto-approve all write operations.
        Future: implement actual rules engine (e.g., "create_event only if calendar is user's own")
        """
        # TODO: Implement rules engine in later step
        return PolicyDecision(allowed=True, reason="Write operation, auto-approved (no rules yet)")
```

### 3. Security Audit Log (via existing `messages` table)

Policy evaluations are stored in the existing `messages` table using `sender='security'` and `message_type='security_audit'`. The structured data goes in the `metadata` JSON column. No new tables needed.

This reuses the existing storage infrastructure. The `messages` table already has indexes on `timestamp` and `chat_jid`, and the `sender` column makes it easy to query or prune security entries independently of chat history.

```python
"""Security audit logging via the existing messages table."""

from __future__ import annotations

import json
import time
from typing import Any

from pynchy.db.messages import store_message_direct


async def record_security_event(
    chat_jid: str,
    workspace: str,
    tool_name: str,
    decision: str,  # 'allowed', 'denied', 'approval_requested', 'rate_limited'
    *,
    tier: str | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    approval_code: str | None = None,
) -> None:
    """Record a policy evaluation in the messages table."""
    metadata = {
        "workspace": workspace,
        "tool_name": tool_name,
        "decision": decision,
        "tier": tier,
        "reason": reason,
        "request_id": request_id,
        "approval_code": approval_code,
    }
    # Strip None values for cleaner storage
    metadata = {k: v for k, v in metadata.items() if v is not None}

    await store_message_direct(
        id=f"audit-{request_id or int(time.time() * 1000)}",
        chat_jid=chat_jid,
        sender="security",
        sender_name="security",
        content=json.dumps(metadata),
        timestamp=time.time(),
        is_from_me=True,
        message_type="security_audit",
        metadata=json.dumps(metadata),
    )
```

Retention pruning uses a simple query scoped to security rows only — chat history is untouched:

```python
async def prune_security_audit(retention_days: int = 30) -> int:
    """Delete security audit entries older than retention period."""
    cutoff_ts = time.time() - (retention_days * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff_ts).isoformat()
    cursor = await db.execute(
        "DELETE FROM messages WHERE sender = 'security' AND timestamp < ?",
        (cutoff_iso,),
    )
    await db.commit()
    return cursor.rowcount
```

### 4. Integrate Policy Middleware into IPC Watcher

**File:** `src/pynchy/ipc/watcher.py` (or wherever IPC files are processed)

```python
from pynchy.policy.audit import record_security_event
from pynchy.policy.middleware import PolicyDeniedError, PolicyMiddleware, PolicyDecision

class IPCWatcher:
    def __init__(self, group_config: dict):
        self.group_config = group_config
        self.workspace_name = group_config.get("name", "unknown")
        self.chat_jid = group_config.get("jid", "unknown")

        # Initialize policy middleware
        security_profile = group_config.get("security_profile")
        if security_profile:
            self.policy = PolicyMiddleware(security_profile)
        else:
            self.policy = None

    async def _audit(self, tool_name: str, decision: str, **kwargs):
        """Record a security event in the messages table."""
        await record_security_event(
            chat_jid=self.chat_jid,
            workspace=self.workspace_name,
            tool_name=tool_name,
            decision=decision,
            **kwargs,
        )

    async def process_ipc_request(self, request_file: Path):
        """Process an IPC request with policy enforcement."""
        with open(request_file) as f:
            request = json.load(f)

        tool_name = request.get("type")
        request_id = request.get("request_id")

        # Apply policy if enabled
        if self.policy:
            decision = self.policy.evaluate(tool_name, request)

            # Determine tier for audit log
            tool_profile = self.policy.security_profile["tools"].get(tool_name)
            tier = tool_profile["tier"] if tool_profile else self.policy.security_profile["default_tier"]

            if not decision.allowed:
                if decision.requires_approval:
                    await self._audit(tool_name, "approval_requested", tier=str(tier), reason=decision.reason, request_id=request_id)
                    await self._request_approval(tool_name, request, request_id)
                    return
                else:
                    audit_decision = "rate_limited" if "rate limit" in (decision.reason or "") else "denied"
                    await self._audit(tool_name, audit_decision, tier=str(tier) if tool_profile else None, reason=decision.reason, request_id=request_id)
                    await self._send_error_response(request_id, f"Policy denied: {decision.reason}")
                    return

            await self._audit(tool_name, "allowed", tier=str(tier), reason=decision.reason, request_id=request_id)

        # Allowed - process the request
        await self._process_allowed_request(tool_name, request, request_id)

    async def _request_approval(self, tool_name: str, request: dict, request_id: str):
        """Request human approval for high-risk action."""
        # TODO: Implement in Step 6 (Human Approval Gate)
        # For now, just deny
        await self._send_error_response(
            request_id, "Human approval not yet implemented (coming in Step 6)"
        )

    async def _send_error_response(self, request_id: str, error_msg: str):
        """Send error response back to agent."""
        response_file = Path(f"/workspace/ipc/input/{request_id}_response.json")
        with open(response_file, "w") as f:
            json.dump({"error": error_msg}, f)

    async def _process_allowed_request(self, tool_name: str, request: dict, request_id: str):
        """Process an allowed request (to be implemented in service integration steps)."""
        # TODO: Implement actual service handlers in Steps 3-5
        # For now, return mock response
        response_file = Path(f"/workspace/ipc/input/{request_id}_response.json")
        with open(response_file, "w") as f:
            json.dump({
                "result": f"Mock response for {tool_name} (service integration coming in Steps 3-5)"
            }, f)
```

### 5. Non-Retryable Policy Denials

Policy denials are deterministic — retrying won't change the outcome. The `PolicyDeniedError` exception type allows the GroupQueue to distinguish policy failures from transient errors.

When the container process exits because of a policy denial, the host should detect the `Policy denied:` prefix in the error response and skip retry scheduling. The GroupQueue's `_schedule_retry()` should check for this:

```python
# In group_queue.py, when handling container errors:
if error_message and error_message.startswith("Policy denied:"):
    # Deterministic failure — do not retry
    logger.warning(f"Policy denial for {group_folder}, not retrying: {error_message}")
    return
# ... existing retry logic ...
```

This prevents wasting 5 retry attempts (with exponential backoff) on failures that will never succeed.

## Tests

**File:** `tests/test_policy_middleware.py`

```python
"""Tests for policy middleware."""

import pytest

from pynchy.policy.middleware import ActionTracker, PolicyMiddleware
from pynchy.types.security import RiskTier


def test_policy_read_only_auto_approved():
    """Test read-only tools are auto-approved."""
    profile = {
        "tools": {
            "read_email": {"tier": RiskTier.READ_ONLY, "enabled": True}
        },
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": False,
        "rate_limits": None,
    }

    policy = PolicyMiddleware(profile)
    decision = policy.evaluate("read_email", {})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_policy_external_requires_approval():
    """Test external tools require approval."""
    profile = {
        "tools": {
            "send_email": {"tier": RiskTier.EXTERNAL, "enabled": True}
        },
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": False,
        "rate_limits": None,
    }

    policy = PolicyMiddleware(profile)
    decision = policy.evaluate("send_email", {"to": "test@example.com"})

    assert decision.allowed is False
    assert decision.requires_approval is True


def test_policy_disabled_tool():
    """Test disabled tools are rejected."""
    profile = {
        "tools": {
            "send_email": {"tier": RiskTier.EXTERNAL, "enabled": False}
        },
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": False,
        "rate_limits": None,
    }

    policy = PolicyMiddleware(profile)
    decision = policy.evaluate("send_email", {})

    assert decision.allowed is False
    assert "disabled" in decision.reason.lower()


def test_policy_unknown_tool_not_allowed():
    """Test unknown tools rejected when allow_unknown_tools=False."""
    profile = {
        "tools": {},
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": False,
        "rate_limits": None,
    }

    policy = PolicyMiddleware(profile)
    decision = policy.evaluate("unknown_tool", {})

    assert decision.allowed is False
    assert "not in security profile" in decision.reason


def test_policy_unknown_tool_uses_default():
    """Test unknown tools use default tier when allowed."""
    profile = {
        "tools": {},
        "default_tier": RiskTier.READ_ONLY,
        "allow_unknown_tools": True,
        "rate_limits": None,
    }

    policy = PolicyMiddleware(profile)
    decision = policy.evaluate("new_tool", {})

    assert decision.allowed is True  # READ_ONLY tier auto-approves


def test_policy_write_tier():
    """Test write-tier tools (auto-approved for now)."""
    profile = {
        "tools": {
            "create_event": {"tier": RiskTier.WRITE, "enabled": True}
        },
        "default_tier": RiskTier.EXTERNAL,
        "allow_unknown_tools": False,
        "rate_limits": None,
    }

    policy = PolicyMiddleware(profile)
    decision = policy.evaluate("create_event", {})

    assert decision.allowed is True  # Auto-approved until rules engine added
    assert decision.requires_approval is False


def test_rate_limit_global():
    """Test global rate limit blocks after threshold."""
    profile = {
        "tools": {
            "read_email": {"tier": RiskTier.READ_ONLY, "enabled": True}
        },
        "default_tier": RiskTier.READ_ONLY,
        "allow_unknown_tools": True,
        "rate_limits": {
            "max_calls_per_hour": 3,
            "per_tool_overrides": {},
        },
    }

    policy = PolicyMiddleware(profile)

    # First 3 calls succeed
    for _ in range(3):
        decision = policy.evaluate("read_email", {})
        assert decision.allowed is True

    # 4th call is rate-limited
    decision = policy.evaluate("read_email", {})
    assert decision.allowed is False
    assert "rate limit" in decision.reason.lower()


def test_rate_limit_per_tool():
    """Test per-tool rate limit blocks specific tool."""
    profile = {
        "tools": {
            "read_email": {"tier": RiskTier.READ_ONLY, "enabled": True},
            "list_calendar": {"tier": RiskTier.READ_ONLY, "enabled": True},
        },
        "default_tier": RiskTier.READ_ONLY,
        "allow_unknown_tools": True,
        "rate_limits": {
            "max_calls_per_hour": 100,  # High global limit
            "per_tool_overrides": {"read_email": 2},
        },
    }

    policy = PolicyMiddleware(profile)

    # 2 read_email calls succeed
    for _ in range(2):
        decision = policy.evaluate("read_email", {})
        assert decision.allowed is True

    # 3rd read_email is blocked
    decision = policy.evaluate("read_email", {})
    assert decision.allowed is False
    assert "read_email" in decision.reason

    # But list_calendar still works
    decision = policy.evaluate("list_calendar", {})
    assert decision.allowed is True


def test_rate_limit_checked_before_tier():
    """Test rate limit is checked even for auto-approved tools."""
    profile = {
        "tools": {
            "read_email": {"tier": RiskTier.READ_ONLY, "enabled": True}
        },
        "default_tier": RiskTier.READ_ONLY,
        "allow_unknown_tools": True,
        "rate_limits": {
            "max_calls_per_hour": 1,
            "per_tool_overrides": {},
        },
    }

    policy = PolicyMiddleware(profile)

    decision = policy.evaluate("read_email", {})
    assert decision.allowed is True

    # Even though read_only is auto-approved, rate limit blocks it
    decision = policy.evaluate("read_email", {})
    assert decision.allowed is False
```

**File:** `tests/test_security_audit.py`

```python
"""Tests for security audit logging."""

import json

import pytest

from pynchy.policy.audit import record_security_event, prune_security_audit


@pytest.mark.asyncio
async def test_record_security_event():
    """Test recording a security event stores it in messages table."""
    await record_security_event(
        chat_jid="group@test",
        workspace="main",
        tool_name="read_email",
        decision="allowed",
        tier="read_only",
        reason="Auto-approved",
        request_id="req-123",
    )

    # Query messages table for security entries
    rows = await db.execute(
        "SELECT * FROM messages WHERE sender = 'security'"
    )
    entries = await rows.fetchall()
    assert len(entries) == 1

    metadata = json.loads(entries[0]["metadata"])
    assert metadata["tool_name"] == "read_email"
    assert metadata["decision"] == "allowed"


@pytest.mark.asyncio
async def test_prune_security_audit():
    """Test retention pruning only deletes security rows."""
    # Insert a security audit entry with old timestamp
    await store_message_direct(
        id="audit-old",
        chat_jid="group@test",
        sender="security",
        sender_name="security",
        content="{}",
        timestamp="2020-01-01T00:00:00",  # Very old
        is_from_me=True,
        message_type="security_audit",
    )

    # Insert a regular chat message with old timestamp
    await store_message_direct(
        id="chat-old",
        chat_jid="group@test",
        sender="user",
        sender_name="User",
        content="Hello",
        timestamp="2020-01-01T00:00:00",
        is_from_me=False,
        message_type="user",
    )

    deleted = await prune_security_audit(retention_days=1)
    assert deleted == 1  # Only the security row

    # Chat message still exists
    rows = await db.execute(
        "SELECT * FROM messages WHERE sender = 'user'"
    )
    assert len(await rows.fetchall()) == 1
```

**File:** `tests/test_ipc_mcp_tools.py`

```python
"""Tests for new IPC MCP tools."""

import json
from pathlib import Path

import pytest

# Test that tools create proper IPC requests
# These are integration tests - may need to mock filesystem


@pytest.mark.asyncio
async def test_send_email_tool(tmp_path, monkeypatch):
    """Test send_email tool creates IPC request."""
    # Mock /workspace/ipc/output to use tmp_path
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setenv("IPC_OUTPUT_DIR", str(output_dir))

    # TODO: Import and call the tool
    # Verify it creates a file in output_dir with correct structure


@pytest.mark.asyncio
async def test_read_email_tool(tmp_path, monkeypatch):
    """Test read_email tool creates IPC request."""
    # Similar to above
    pass


# Add tests for other tools...
```

## Success Criteria

- [ ] New MCP tools implemented (email, calendar, password manager)
- [ ] Policy middleware created with tier-based evaluation and rate limiting
- [ ] Security audit events stored in existing `messages` table (`sender='security'`)
- [ ] Retention pruning scoped to security rows (`WHERE sender = 'security'`)
- [ ] Policy denials marked as non-retryable in GroupQueue
- [ ] IPC watcher integrated with policy enforcement and audit logging
- [ ] Tests pass (policy logic, rate limiting, audit log, tool creation, integration)
- [ ] Mock responses work for testing
- [ ] Documentation updated with new tool schemas

## Documentation

Update the following:

1. **IPC protocol docs** - Document new request types and payloads
2. **MCP tools reference** - Add new tools with examples
3. **Security enforcement** - Explain how policy middleware, rate limiting, and audit log work
4. **Operations** - Audit log retention configuration and manual pruning

## Notes

- This step does NOT implement actual service integrations (email, calendar, etc.)
- Tools write IPC requests that return mock responses
- Steps 3-5 will implement real service handlers
- Step 6 will implement human approval flow
- Rules engine (for write-tier tools) is placeholder - can be enhanced later
- Security audit entries live in the existing `messages` table (`sender='security'`, `message_type='security_audit'`), no new tables needed. Prunable with `DELETE FROM messages WHERE sender = 'security' AND timestamp < cutoff` — chat history is untouched
- Policy denials use a distinguishable error prefix (`Policy denied:`) so the GroupQueue can skip retry scheduling

## Next Steps

After this is complete:
- Step 6: Human approval gate (WhatsApp approval flow) — **do this before service integrations**
- Step 3: Email service integration (IMAP/SMTP adapter)
- Step 4: Calendar service integration (CalDAV/Google Calendar)
- Step 5: Password manager integration (1Password CLI)
