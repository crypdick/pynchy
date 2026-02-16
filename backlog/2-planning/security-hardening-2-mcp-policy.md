# Security Hardening: Step 2 - MCP Tools & Taint-Aware Policy

## Overview

Implement new MCP tools for external services (email, calendar, passwords) and add taint-aware policy middleware that gates tool execution based on per-service trust declarations (`ServiceTrustConfig`) and container taint state.

## Scope

This step extends the container's MCP server with new IPC tools and adds the policy enforcement layer that evaluates IPC requests against the tainted container model. Includes rate limiting, taint tracking, audit logging (via existing `messages` table), and non-retryable denial classification. Does NOT implement actual service integrations yet - tools will write IPC requests that later steps will process.

## Dependencies

- Step 1: Service Trust Profiles (must be complete)
- IPC MCP system (already implemented)

## Implementation

### 1. Extend IPC MCP Tools

**File:** `src/pynchy/agent_runner/ipc_mcp.py`

Each IPC request carries its **service name** explicitly. There is no tool-to-service mapping table on the host side -- the container knows which service it is calling and declares it in the request payload.

Add new MCP tools that write IPC requests:

```python
# Email tools
@server.call_tool()
async def read_email(arguments: dict) -> list[TextContent]:
    """Read emails matching filter criteria."""
    request = {
        "type": "read_email",
        "service": "email",
        "folder": arguments.get("folder", "INBOX"),
        "limit": arguments.get("limit", 10),
        "unread_only": arguments.get("unread_only", False),
    }
    return await write_ipc_request("read_email", request)


@server.call_tool()
async def send_email(arguments: dict) -> list[TextContent]:
    """Send an email (requires approval when tainted)."""
    request = {
        "type": "send_email",
        "service": "email",
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
        "service": "calendar",
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
        "service": "calendar",
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
    """Delete a calendar event (requires approval when tainted)."""
    request = {
        "type": "delete_event",
        "service": "calendar",
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
        "service": "passwords",
        "query": arguments["query"],
    }
    return await write_ipc_request("search_passwords", request)


@server.call_tool()
async def get_password(arguments: dict) -> list[TextContent]:
    """Get password from vault (requires approval when tainted)."""
    request = {
        "type": "get_password",
        "service": "passwords",
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

### 2. Create Policy Middleware (Taint-Aware)

**File:** `src/pynchy/policy/middleware.py` (new file)

The middleware evaluates IPC requests using the tainted container model. There are no per-tool tiers or tool-to-service mapping tables. Each IPC request carries its own `service` field, and the middleware looks up the `ServiceTrustConfig` for that service.

**Evaluation flow:**

1. Check rate limits (always, regardless of trust)
2. Look up `ServiceTrustConfig` for the service name in the request
3. If `trusted_source=false` and this is a read: allow, but flag for deputy scan and mark the container tainted
4. If the container is tainted and `trusted_sink=false`: require deputy review + human approval
5. If the container is tainted and `sensitive_info=true`: require human approval
6. If the service is fully trusted: allow without gating
7. Unknown services: default to max restriction `{trusted_source: false, sensitive_info: true, trusted_sink: false}`

```python
"""Taint-aware policy enforcement middleware for IPC requests."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from pynchy.types.security import RateLimitConfig, ServiceTrustConfig

logger = logging.getLogger(__name__)

# Default trust config for unknown services — maximally restrictive
UNKNOWN_SERVICE_TRUST = ServiceTrustConfig(
    trusted_source=False,
    sensitive_info=True,
    trusted_sink=False,
)

# Which IPC request types are "sinks" (write/send to external systems)
SINK_OPERATIONS: frozenset[str] = frozenset({
    "send_email", "create_event", "delete_event",
})


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
        requires_deputy_scan: bool = False,
        taints_container: bool = False,
    ):
        self.allowed = allowed
        self.reason = reason
        self.requires_approval = requires_approval
        self.requires_deputy_scan = requires_deputy_scan
        self.taints_container = taints_container


class ActionTracker:
    """Sliding window rate limiter for tool calls."""

    def __init__(self, rate_limits: RateLimitConfig):
        self.rate_limits = rate_limits
        self._timestamps: list[float] = []
        self._per_tool: dict[str, list[float]] = defaultdict(list)
        self._window_seconds = 3600  # 1 hour

    def _prune(self, timestamps: list[float], now: float) -> list[float]:
        cutoff = now - self._window_seconds
        return [t for t in timestamps if t > cutoff]

    def check_and_record(self, tool_name: str) -> tuple[bool, str | None]:
        now = time.monotonic()
        self._timestamps = self._prune(self._timestamps, now)
        self._per_tool[tool_name] = self._prune(self._per_tool[tool_name], now)

        if len(self._timestamps) >= self.rate_limits["max_calls_per_hour"]:
            return False, (
                f"Global rate limit exceeded: {self.rate_limits['max_calls_per_hour']} calls/hour"
            )

        per_tool_limit = self.rate_limits.get("per_tool_overrides", {}).get(tool_name)
        if per_tool_limit and len(self._per_tool[tool_name]) >= per_tool_limit:
            return False, (
                f"Per-tool rate limit exceeded for {tool_name}: {per_tool_limit} calls/hour"
            )

        self._timestamps.append(now)
        self._per_tool[tool_name].append(now)
        return True, None


class PolicyMiddleware:
    """Evaluates IPC requests against tainted container model.

    Uses ServiceTrustConfig per service + container taint state to decide
    whether to allow, gate, or deny each request. No per-tool tiers —
    gating is derived entirely from service trust declarations and taint.
    """

    def __init__(
        self,
        services: dict[str, ServiceTrustConfig],
        rate_limits: RateLimitConfig | None = None,
    ):
        self.services = services
        self.tainted = False
        self.tracker: ActionTracker | None = None

        if rate_limits:
            self.tracker = ActionTracker(rate_limits)

    def _get_trust(self, service_name: str) -> ServiceTrustConfig:
        """Look up trust config for a service. Unknown services get max restriction."""
        return self.services.get(service_name, UNKNOWN_SERVICE_TRUST)

    def evaluate(self, tool_name: str, request: dict) -> PolicyDecision:
        """Evaluate whether an IPC request should be allowed.

        Order: rate limit -> service trust + taint state.
        The service name comes from the request payload (request["service"]).
        """
        # 1. Rate limits (always checked)
        if self.tracker:
            allowed, reason = self.tracker.check_and_record(tool_name)
            if not allowed:
                return PolicyDecision(allowed=False, reason=reason)

        # 2. Look up service trust config
        service_name = request.get("service")
        if not service_name:
            # No service declared — treat as unknown (max restriction)
            service_name = "__unknown__"

        trust = self._get_trust(service_name)
        is_sink = tool_name in SINK_OPERATIONS

        # 3. Fully trusted service: allow without gating
        if trust.trusted_source and not trust.sensitive_info and trust.trusted_sink:
            return PolicyDecision(
                allowed=True,
                reason=f"Fully trusted service: {service_name}",
            )

        # 4. Read from untrusted source: allow, but deputy scan + taint
        if not is_sink and not trust.trusted_source:
            return PolicyDecision(
                allowed=True,
                reason=f"Untrusted source ({service_name}) — deputy scan required, container will be tainted",
                requires_deputy_scan=True,
                taints_container=True,
            )

        # 5. Tainted container writing to untrusted sink: deputy + human approval
        if self.tainted and is_sink and not trust.trusted_sink:
            return PolicyDecision(
                allowed=False,
                reason=f"Tainted container writing to untrusted sink ({service_name}) — deputy review + human approval required",
                requires_approval=True,
                requires_deputy_scan=True,
            )

        # 6. Tainted container accessing sensitive data: human approval
        if self.tainted and trust.sensitive_info:
            return PolicyDecision(
                allowed=False,
                reason=f"Tainted container accessing sensitive service ({service_name}) — human approval required",
                requires_approval=True,
            )

        # 7. Non-tainted container, non-fully-trusted service: allow
        #    (e.g., writing to an untrusted sink when not tainted is fine,
        #     or reading sensitive data when not tainted is fine)
        return PolicyDecision(
            allowed=True,
            reason=f"Allowed: service={service_name}, tainted={self.tainted}, is_sink={is_sink}",
        )

    def mark_tainted(self) -> None:
        """Mark the container as tainted (has seen untrusted input)."""
        if not self.tainted:
            logger.info("Container marked as tainted")
            self.tainted = True
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
    decision: str,  # 'allowed', 'denied', 'approval_requested', 'rate_limited', 'tainted'
    *,
    service: str | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    approval_code: str | None = None,
    taints_container: bool = False,
) -> None:
    """Record a policy evaluation in the messages table."""
    metadata = {
        "workspace": workspace,
        "tool_name": tool_name,
        "decision": decision,
        "service": service,
        "reason": reason,
        "request_id": request_id,
        "approval_code": approval_code,
        "taints_container": taints_container,
    }
    # Strip None/False values for cleaner storage
    metadata = {k: v for k, v in metadata.items() if v}

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

Retention pruning uses a simple query scoped to security rows only -- chat history is untouched:

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

The watcher reads the `service` field from each IPC request and passes it through to the middleware. After evaluation, if the decision taints the container, the watcher calls `mark_tainted()`.

```python
from pynchy.policy.audit import record_security_event
from pynchy.policy.middleware import PolicyDeniedError, PolicyMiddleware, PolicyDecision

class IPCWatcher:
    def __init__(self, group_config: dict):
        self.group_config = group_config
        self.workspace_name = group_config.get("name", "unknown")
        self.chat_jid = group_config.get("jid", "unknown")

        # Initialize policy middleware from service trust declarations
        security_profile = group_config.get("security_profile")
        if security_profile:
            self.policy = PolicyMiddleware(
                services=security_profile.get("services", {}),
                rate_limits=security_profile.get("rate_limits"),
            )
        else:
            self.policy = None

    async def _audit(self, tool_name: str, decision_type: str, **kwargs):
        """Record a security event in the messages table."""
        await record_security_event(
            chat_jid=self.chat_jid,
            workspace=self.workspace_name,
            tool_name=tool_name,
            decision=decision_type,
            **kwargs,
        )

    async def process_ipc_request(self, request_file: Path):
        """Process an IPC request with policy enforcement."""
        with open(request_file) as f:
            request = json.load(f)

        tool_name = request.get("type")
        request_id = request.get("request_id")
        service_name = request.get("service")

        # Apply policy if enabled
        if self.policy:
            decision = self.policy.evaluate(tool_name, request)

            if not decision.allowed:
                if decision.requires_approval:
                    await self._audit(
                        tool_name, "approval_requested",
                        service=service_name, reason=decision.reason,
                        request_id=request_id,
                    )
                    await self._request_approval(tool_name, request, request_id)
                    return
                else:
                    audit_type = "rate_limited" if "rate limit" in (decision.reason or "") else "denied"
                    await self._audit(
                        tool_name, audit_type,
                        service=service_name, reason=decision.reason,
                        request_id=request_id,
                    )
                    await self._send_error_response(request_id, f"Policy denied: {decision.reason}")
                    return

            # Allowed — apply side effects
            if decision.taints_container:
                self.policy.mark_tainted()
                await self._audit(
                    tool_name, "tainted",
                    service=service_name,
                    reason="Container tainted by untrusted source read",
                    request_id=request_id,
                    taints_container=True,
                )

            await self._audit(
                tool_name, "allowed",
                service=service_name, reason=decision.reason,
                request_id=request_id,
            )

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

Policy denials are deterministic -- retrying won't change the outcome. The `PolicyDeniedError` exception type allows the GroupQueue to distinguish policy failures from transient errors.

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
"""Tests for taint-aware policy middleware."""

import pytest

from pynchy.policy.middleware import ActionTracker, PolicyMiddleware, UNKNOWN_SERVICE_TRUST
from pynchy.types.security import ServiceTrustConfig


# --- Fixtures ---

def _email_trust():
    return ServiceTrustConfig(trusted_source=False, sensitive_info=False, trusted_sink=False)

def _calendar_trust():
    return ServiceTrustConfig(trusted_source=True, sensitive_info=False, trusted_sink=True)

def _passwords_trust():
    return ServiceTrustConfig(trusted_source=True, sensitive_info=True, trusted_sink=False)


def _make_middleware(**kwargs):
    services = kwargs.pop("services", {
        "email": _email_trust(),
        "calendar": _calendar_trust(),
        "passwords": _passwords_trust(),
    })
    return PolicyMiddleware(services=services, **kwargs)


# --- Fully trusted service tests ---

def test_fully_trusted_service_allows_read():
    """Fully trusted service (calendar) allows reads without gating."""
    mw = _make_middleware()
    decision = mw.evaluate("list_calendar", {"service": "calendar"})

    assert decision.allowed is True
    assert decision.requires_deputy_scan is False
    assert decision.requires_approval is False
    assert decision.taints_container is False


def test_fully_trusted_service_allows_write():
    """Fully trusted service (calendar) allows writes without gating."""
    mw = _make_middleware()
    decision = mw.evaluate("create_event", {"service": "calendar"})

    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.taints_container is False


def test_fully_trusted_service_allows_write_even_when_tainted():
    """Fully trusted service allows writes even if container is tainted."""
    mw = _make_middleware()
    mw.mark_tainted()
    decision = mw.evaluate("create_event", {"service": "calendar"})

    assert decision.allowed is True
    assert decision.requires_approval is False


# --- Untrusted source (read) tests ---

def test_untrusted_source_read_allows_but_taints():
    """Reading from untrusted source is allowed but flags deputy scan and taints."""
    mw = _make_middleware()
    decision = mw.evaluate("read_email", {"service": "email"})

    assert decision.allowed is True
    assert decision.requires_deputy_scan is True
    assert decision.taints_container is True


def test_untrusted_source_read_does_not_require_approval():
    """Reading from untrusted source does not require human approval."""
    mw = _make_middleware()
    decision = mw.evaluate("read_email", {"service": "email"})

    assert decision.requires_approval is False


# --- Tainted container + untrusted sink tests ---

def test_tainted_write_to_untrusted_sink_requires_approval():
    """Tainted container writing to untrusted sink requires deputy + human approval."""
    mw = _make_middleware()
    mw.mark_tainted()
    decision = mw.evaluate("send_email", {"service": "email"})

    assert decision.allowed is False
    assert decision.requires_approval is True
    assert decision.requires_deputy_scan is True


def test_non_tainted_write_to_untrusted_sink_allowed():
    """Non-tainted container can write to untrusted sink without gating."""
    mw = _make_middleware()
    assert not mw.tainted
    decision = mw.evaluate("send_email", {"service": "email"})

    assert decision.allowed is True
    assert decision.requires_approval is False


# --- Tainted container + sensitive data tests ---

def test_tainted_access_sensitive_requires_approval():
    """Tainted container accessing sensitive service requires human approval."""
    mw = _make_middleware()
    mw.mark_tainted()
    decision = mw.evaluate("get_password", {"service": "passwords"})

    assert decision.allowed is False
    assert decision.requires_approval is True


def test_non_tainted_access_sensitive_allowed():
    """Non-tainted container can access sensitive service without gating."""
    mw = _make_middleware()
    assert not mw.tainted
    decision = mw.evaluate("get_password", {"service": "passwords"})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_tainted_read_sensitive_requires_approval():
    """Tainted container reading from sensitive source also requires approval."""
    mw = _make_middleware()
    mw.mark_tainted()
    decision = mw.evaluate("search_passwords", {"service": "passwords"})

    assert decision.allowed is False
    assert decision.requires_approval is True


# --- Taint state management tests ---

def test_mark_tainted():
    """mark_tainted() sets tainted state."""
    mw = _make_middleware()
    assert mw.tainted is False
    mw.mark_tainted()
    assert mw.tainted is True


def test_taint_is_sticky():
    """Once tainted, container stays tainted."""
    mw = _make_middleware()
    mw.mark_tainted()
    assert mw.tainted is True
    mw.mark_tainted()  # idempotent
    assert mw.tainted is True


def test_taint_flow_read_then_send():
    """Full flow: read email (taints) then send email (blocked)."""
    mw = _make_middleware()

    # Read from untrusted source — allowed, taints container
    read_decision = mw.evaluate("read_email", {"service": "email"})
    assert read_decision.allowed is True
    assert read_decision.taints_container is True

    # Simulate host applying the taint
    mw.mark_tainted()

    # Now try to send email — blocked because tainted + untrusted sink
    send_decision = mw.evaluate("send_email", {"service": "email"})
    assert send_decision.allowed is False
    assert send_decision.requires_approval is True


# --- Unknown service tests ---

def test_unknown_service_defaults_to_max_restriction():
    """Unknown services get max restriction (untrusted source, sensitive, untrusted sink)."""
    mw = _make_middleware()
    decision = mw.evaluate("some_tool", {"service": "unknown_service"})

    # Unknown source is untrusted, so read triggers deputy + taint
    assert decision.requires_deputy_scan is True
    assert decision.taints_container is True


def test_unknown_service_tainted_write_blocked():
    """Unknown service sink blocked when tainted."""
    mw = _make_middleware()
    mw.mark_tainted()
    # Unknown service: sensitive_info=True, so tainted access is blocked
    decision = mw.evaluate("unknown_write", {"service": "unknown_service"})
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_missing_service_field_treated_as_unknown():
    """Request with no service field is treated as unknown (max restriction)."""
    mw = _make_middleware()
    decision = mw.evaluate("some_tool", {})  # no "service" key

    assert decision.requires_deputy_scan is True
    assert decision.taints_container is True


# --- Rate limiting tests ---

def test_rate_limit_global():
    """Global rate limit blocks after threshold."""
    mw = _make_middleware(
        rate_limits={
            "max_calls_per_hour": 3,
            "per_tool_overrides": {},
        },
    )

    # First 3 calls succeed
    for _ in range(3):
        decision = mw.evaluate("list_calendar", {"service": "calendar"})
        assert decision.allowed is True

    # 4th call is rate-limited
    decision = mw.evaluate("list_calendar", {"service": "calendar"})
    assert decision.allowed is False
    assert "rate limit" in decision.reason.lower()


def test_rate_limit_per_tool():
    """Per-tool rate limit blocks specific tool while others continue."""
    mw = _make_middleware(
        rate_limits={
            "max_calls_per_hour": 100,
            "per_tool_overrides": {"read_email": 2},
        },
    )

    # 2 read_email calls succeed
    for _ in range(2):
        decision = mw.evaluate("read_email", {"service": "email"})
        assert decision.allowed is True

    # 3rd read_email is blocked
    decision = mw.evaluate("read_email", {"service": "email"})
    assert decision.allowed is False
    assert "read_email" in decision.reason

    # But list_calendar still works
    decision = mw.evaluate("list_calendar", {"service": "calendar"})
    assert decision.allowed is True


def test_rate_limit_checked_before_trust():
    """Rate limit is checked even for fully trusted services."""
    mw = _make_middleware(
        rate_limits={
            "max_calls_per_hour": 1,
            "per_tool_overrides": {},
        },
    )

    decision = mw.evaluate("list_calendar", {"service": "calendar"})
    assert decision.allowed is True

    # Even though calendar is fully trusted, rate limit blocks it
    decision = mw.evaluate("list_calendar", {"service": "calendar"})
    assert decision.allowed is False
    assert "rate limit" in decision.reason.lower()
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
        service="email",
        reason="Untrusted source — deputy scan required",
        request_id="req-123",
        taints_container=True,
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
    assert metadata["service"] == "email"
    assert metadata["taints_container"] is True


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

# Test that tools create proper IPC requests with service field
# These are integration tests - may need to mock filesystem


@pytest.mark.asyncio
async def test_send_email_tool_includes_service(tmp_path, monkeypatch):
    """Test send_email tool creates IPC request with service='email'."""
    # Mock /workspace/ipc/output to use tmp_path
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setenv("IPC_OUTPUT_DIR", str(output_dir))

    # TODO: Import and call the tool
    # Verify it creates a file in output_dir with correct structure
    # Verify request["service"] == "email"


@pytest.mark.asyncio
async def test_list_calendar_tool_includes_service(tmp_path, monkeypatch):
    """Test list_calendar tool creates IPC request with service='calendar'."""
    # Similar to above, verify request["service"] == "calendar"
    pass


@pytest.mark.asyncio
async def test_get_password_tool_includes_service(tmp_path, monkeypatch):
    """Test get_password tool creates IPC request with service='passwords'."""
    # Similar to above, verify request["service"] == "passwords"
    pass


# Add tests for other tools...
```

## Success Criteria

- [ ] New MCP tools implemented (email, calendar, password manager) with `service` field in every request
- [ ] Policy middleware created with taint-aware evaluation (no per-tool tiers)
- [ ] `PolicyDecision` includes `taints_container` field
- [ ] Container taint state tracked in `PolicyMiddleware.tainted`
- [ ] Unknown services default to max restriction `{trusted_source: false, sensitive_info: true, trusted_sink: false}`
- [ ] Security audit events stored in existing `messages` table (`sender='security'`)
- [ ] Retention pruning scoped to security rows (`WHERE sender = 'security'`)
- [ ] Policy denials marked as non-retryable in GroupQueue
- [ ] IPC watcher integrated with policy enforcement, taint tracking, and audit logging
- [ ] Tests pass (taint flow, trust evaluation, rate limiting, audit log, tool creation)
- [ ] Mock responses work for testing
- [ ] Documentation updated with new tool schemas

## Documentation

Update the following:

1. **IPC protocol docs** - Document new request types, payloads, and the required `service` field
2. **MCP tools reference** - Add new tools with examples
3. **Security enforcement** - Explain taint-aware policy middleware and how gating is derived from `ServiceTrustConfig`
4. **Operations** - Audit log retention configuration and manual pruning

## Notes

- This step does NOT implement actual service integrations (email, calendar, etc.)
- Tools write IPC requests that return mock responses
- Steps 3-5 will implement real service handlers
- Step 6 will implement human approval flow
- Deputy agent (for scanning untrusted source content) is a placeholder here -- implemented in Step 7
- Security audit entries live in the existing `messages` table (`sender='security'`, `message_type='security_audit'`), no new tables needed. Prunable with `DELETE FROM messages WHERE sender = 'security' AND timestamp < cutoff` -- chat history is untouched
- Policy denials use a distinguishable error prefix (`Policy denied:`) so the GroupQueue can skip retry scheduling
- Each IPC request carries its own `service` field -- no tool-to-service mapping table needed on the host side

## Next Steps

After this is complete:
- Step 6: Human approval gate (WhatsApp approval flow) -- **do this before service integrations**
- Step 3: Email service integration (IMAP/SMTP adapter)
- Step 4: Calendar service integration (CalDAV/Google Calendar)
- Step 5: Password manager integration (1Password CLI)
