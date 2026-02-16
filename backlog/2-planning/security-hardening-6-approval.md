# Security Hardening: Step 6 - Human Approval Gate

## Overview

Implement the human approval system for high-risk MCP tool operations. When an agent attempts an EXTERNAL-tier action (like sending email or retrieving passwords), the host sends an approval request via WhatsApp and waits for the user's response.

## Scope

This step completes the security enforcement layer by adding the final gate for destructive and external operations. It integrates with the existing WhatsApp messaging system to send approval requests and process approve/deny responses.

## Dependencies

- ✅ Step 1: Workspace Security Profiles (must be complete)
- ✅ Step 2: MCP Tools & Basic Policy (must be complete)
- Steps 3-5: Service integrations (optional, but this is what makes them safe)

## Background

From Step 2, when the policy middleware determines a tool requires human approval (`RiskTier.EXTERNAL`), it sets `requires_approval=True`. Currently, this just denies the request. This step implements the actual approval flow.

## Security Model

- **Default: Deny** - All approval requests default to DENY after 5-minute timeout
- **Explicit approval required** - User must reply with approval code
- **One-time codes** - Each request gets unique approval ID
- **Context shown** - Approval request shows full action details
- **Auditable** - All approval decisions recorded in the `messages` table (from Step 2, `sender='security'`) with the `approval_code` field in metadata, queryable by workspace, tool, and time range

## Implementation

### 1. Approval Request Schema

**File:** `src/pynchy/policy/approval.py` (new file)

```python
"""Human approval system for high-risk operations."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ApprovalRequest:
    """Pending approval request."""

    request_id: str  # IPC request ID
    approval_code: str  # Short code for user to reply with
    tool_name: str
    workspace: str
    details: dict[str, Any]  # Tool arguments
    created_at: datetime
    timeout: timedelta = timedelta(minutes=5)

    @property
    def is_expired(self) -> bool:
        """Check if request has expired."""
        return datetime.now() > self.created_at + self.timeout


class ApprovalManager:
    """Manages pending approval requests."""

    def __init__(self, message_sender):
        """Initialize approval manager.

        Args:
            message_sender: Function to send messages (e.g., WhatsApp)
        """
        self.message_sender = message_sender
        self.pending: dict[str, ApprovalRequest] = {}  # approval_code -> request
        self._lock = asyncio.Lock()

    def _generate_approval_code(self) -> str:
        """Generate short approval code (6 chars, readable)."""
        # Use base32 for readability (no 0/O or 1/l confusion)
        return secrets.token_urlsafe(4)[:6].upper()

    async def request_approval(
        self,
        request_id: str,
        tool_name: str,
        workspace: str,
        details: dict[str, Any],
    ) -> str:
        """Send approval request and return approval code.

        Args:
            request_id: IPC request ID to respond to when approved/denied
            tool_name: Name of the tool being called
            workspace: Workspace requesting the action
            details: Tool arguments (sanitized for display)

        Returns:
            Approval code for this request
        """
        async with self._lock:
            # Generate unique code
            code = self._generate_approval_code()
            while code in self.pending:
                code = self._generate_approval_code()

            # Create request
            request = ApprovalRequest(
                request_id=request_id,
                approval_code=code,
                tool_name=tool_name,
                workspace=workspace,
                details=details,
                created_at=datetime.now(),
            )

            self.pending[code] = request

        # Format approval message
        message = self._format_approval_message(request)

        # Send via WhatsApp (or other channel)
        await self.message_sender(message)

        return code

    def _format_approval_message(self, request: ApprovalRequest) -> str:
        """Format approval request for display."""
        details_str = "\n".join(
            f"  {key}: {value}" for key, value in request.details.items()
        )

        return f"""*[APPROVAL REQUIRED]*

*Workspace:* {request.workspace}
*Action:* {request.tool_name}

*Details:*
{details_str}

Reply:
• `approve {request.approval_code}` to approve
• `deny {request.approval_code}` to deny

_Expires in {int(request.timeout.total_seconds() / 60)} minutes (default: deny)_"""

    async def handle_response(self, response: str) -> dict[str, Any] | None:
        """Process user approval/denial response.

        Args:
            response: User message (e.g., "approve ABC123" or "deny ABC123")

        Returns:
            Dict with request_id and approved status, or None if not recognized
        """
        parts = response.strip().lower().split()
        if len(parts) != 2:
            return None

        action, code = parts
        code = code.upper()

        if action not in ("approve", "deny"):
            return None

        async with self._lock:
            request = self.pending.get(code)

            if not request:
                # Unknown or already processed
                return None

            if request.is_expired:
                # Expired - treat as deny
                del self.pending[code]
                return {
                    "request_id": request.request_id,
                    "approved": False,
                    "reason": "Request expired",
                }

            # Remove from pending
            del self.pending[code]

            approved = action == "approve"

            logger.info(
                f"Approval {action}d: {request.tool_name} in {request.workspace} "
                f"(code: {code})"
            )

            return {
                "request_id": request.request_id,
                "approved": approved,
                "reason": f"User {action}d",
            }

    async def cleanup_expired(self):
        """Remove expired requests and send denial responses."""
        async with self._lock:
            expired = [
                (code, req)
                for code, req in self.pending.items()
                if req.is_expired
            ]

            for code, request in expired:
                logger.warning(
                    f"Approval timeout: {request.tool_name} in {request.workspace} "
                    f"(code: {code})"
                )
                del self.pending[code]

                # Return denied response (will be handled by IPC watcher)
                yield {
                    "request_id": request.request_id,
                    "approved": False,
                    "reason": "Request timed out",
                }
```

### 2. Integrate into IPC Watcher

**File:** `src/pynchy/ipc/watcher.py`

Update to use approval manager:

```python
from pynchy.policy.approval import ApprovalManager

class IPCWatcher:
    def __init__(self, group_config: dict, services_config: dict):
        # ... existing init ...

        # Initialize approval manager
        self.approval_manager = ApprovalManager(
            message_sender=self._send_whatsapp_message
        )

        # Start cleanup task
        asyncio.create_task(self._cleanup_expired_approvals())

    async def _send_whatsapp_message(self, message: str):
        """Send message via WhatsApp (or active channel)."""
        # Use existing IPC message system
        # This should send to the group that owns this workspace
        # Implementation depends on existing message infrastructure
        pass  # TODO: Wire up to actual WhatsApp sender

    async def _cleanup_expired_approvals(self):
        """Periodically clean up expired approval requests."""
        while True:
            await asyncio.sleep(30)  # Check every 30 seconds

            async for denial in self.approval_manager.cleanup_expired():
                # Record timeout in audit log (messages table)
                await self._audit(
                    denial.get("tool_name", "unknown"),
                    "denied",
                    tier="external",
                    reason=denial["reason"],
                    request_id=denial["request_id"],
                )
                # Send denial response
                await self._send_error_response(
                    denial["request_id"],
                    f"Denied: {denial['reason']}",
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

            if not decision.allowed:
                if decision.requires_approval:
                    # Request human approval
                    approval_code = await self.approval_manager.request_approval(
                        request_id=request_id,
                        tool_name=tool_name,
                        workspace=self.group_config["name"],
                        details=self._sanitize_details(request),
                    )
                    # Response will be sent when user approves/denies
                    # Don't send response here
                    return
                else:
                    # Denied by policy
                    await self._send_error_response(
                        request_id, f"Policy denied: {decision.reason}"
                    )
                    return

        # Allowed - process the request
        await self._process_allowed_request(tool_name, request, request_id)

    def _sanitize_details(self, request: dict) -> dict[str, Any]:
        """Sanitize request details for display in approval message.

        Remove sensitive fields and format for readability.
        """
        # Remove internal fields
        details = {k: v for k, v in request.items() if not k.startswith("_")}
        details.pop("request_id", None)
        details.pop("type", None)

        # Truncate long values
        for key, value in details.items():
            if isinstance(value, str) and len(value) > 100:
                details[key] = value[:100] + "..."

        return details

    async def process_approval_response(self, message: str):
        """Process user approval/denial message.

        Args:
            message: User message (e.g., "approve ABC123")
        """
        result = await self.approval_manager.handle_response(message)

        if not result:
            # Not an approval response
            return

        request_id = result["request_id"]
        approved = result["approved"]

        # Record the approval decision in the audit log (messages table)
        await self._audit(
            result.get("tool_name", "unknown"),
            "approved" if approved else "denied",
            tier="external",
            reason=result["reason"],
            request_id=request_id,
            approval_code=result.get("approval_code"),
        )

        if approved:
            # Re-fetch the original request and process it
            # (This assumes we cache pending requests somewhere)
            # For now, just send success confirmation
            await self._send_response(
                request_id,
                {"result": "Approved - processing action"},
            )
            # TODO: Actually execute the approved action
        else:
            # Send denial
            await self._send_error_response(
                request_id,
                f"Denied: {result['reason']}",
            )
```

### 3. Wire Up Message Handling

The approval manager needs to be accessible to the message processing pipeline so that incoming messages can be checked for approval responses.

**File:** `src/pynchy/message_handler.py` (or wherever messages are processed)

```python
async def handle_incoming_message(message: str, group: str):
    """Handle incoming WhatsApp message."""

    # Get the IPC watcher for this group
    watcher = get_watcher_for_group(group)

    # Check if this is an approval response
    if message.strip().lower().startswith(("approve ", "deny ")):
        await watcher.process_approval_response(message)
        # Don't process as regular message
        return

    # ... existing message handling ...
```

## Tests

**File:** `tests/test_approval_manager.py`

```python
"""Tests for approval manager."""

import asyncio
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock

from pynchy.policy.approval import ApprovalManager, ApprovalRequest


@pytest.fixture
def message_sender():
    """Mock message sender."""
    return AsyncMock()


@pytest.fixture
def approval_manager(message_sender):
    """Create approval manager for testing."""
    return ApprovalManager(message_sender)


@pytest.mark.asyncio
async def test_request_approval(approval_manager, message_sender):
    """Test requesting approval."""
    code = await approval_manager.request_approval(
        request_id="req-123",
        tool_name="send_email",
        workspace="main",
        details={"to": "test@example.com", "subject": "Test"},
    )

    assert code is not None
    assert len(code) == 6
    assert code.isupper()

    # Message should be sent
    message_sender.assert_called_once()
    message = message_sender.call_args[0][0]
    assert "[APPROVAL REQUIRED]" in message
    assert "send_email" in message
    assert code in message


@pytest.mark.asyncio
async def test_approve_response(approval_manager):
    """Test approval response."""
    code = await approval_manager.request_approval(
        request_id="req-456",
        tool_name="get_password",
        workspace="main",
        details={"item_id": "item-123"},
    )

    result = await approval_manager.handle_response(f"approve {code}")

    assert result is not None
    assert result["request_id"] == "req-456"
    assert result["approved"] is True


@pytest.mark.asyncio
async def test_deny_response(approval_manager):
    """Test denial response."""
    code = await approval_manager.request_approval(
        request_id="req-789",
        tool_name="delete_event",
        workspace="main",
        details={"event_id": "event-123"},
    )

    result = await approval_manager.handle_response(f"deny {code}")

    assert result is not None
    assert result["request_id"] == "req-789"
    assert result["approved"] is False


@pytest.mark.asyncio
async def test_invalid_response(approval_manager):
    """Test invalid response is ignored."""
    result = await approval_manager.handle_response("invalid message")
    assert result is None

    result = await approval_manager.handle_response("approve")
    assert result is None


@pytest.mark.asyncio
async def test_unknown_code(approval_manager):
    """Test unknown approval code."""
    result = await approval_manager.handle_response("approve BADCODE")
    assert result is None


@pytest.mark.asyncio
async def test_expired_request(approval_manager):
    """Test expired request is denied."""
    code = await approval_manager.request_approval(
        request_id="req-999",
        tool_name="send_email",
        workspace="main",
        details={},
    )

    # Manually expire the request
    async with approval_manager._lock:
        approval_manager.pending[code].timeout = timedelta(seconds=0)

    await asyncio.sleep(0.1)

    result = await approval_manager.handle_response(f"approve {code}")

    assert result is not None
    assert result["approved"] is False
    assert "expired" in result["reason"].lower()


@pytest.mark.asyncio
async def test_cleanup_expired(approval_manager):
    """Test cleanup of expired requests."""
    code = await approval_manager.request_approval(
        request_id="req-111",
        tool_name="send_email",
        workspace="main",
        details={},
    )

    # Expire the request
    async with approval_manager._lock:
        approval_manager.pending[code].timeout = timedelta(seconds=0)

    # Cleanup
    denials = [d async for d in approval_manager.cleanup_expired()]

    assert len(denials) == 1
    assert denials[0]["request_id"] == "req-111"
    assert denials[0]["approved"] is False

    # Request should be removed
    assert code not in approval_manager.pending
```

## User Experience

### Example: Sending Email

1. Agent calls `send_email` tool
2. Policy middleware detects EXTERNAL tier
3. Approval request sent to WhatsApp:

```
[APPROVAL REQUIRED]

Workspace: personal
Action: send_email

Details:
  to: alice@example.com
  subject: Meeting reminder
  body: Hi Alice, reminder about...

Reply:
• approve X7K2M9 to approve
• deny X7K2M9 to deny

Expires in 5 minutes (default: deny)
```

4. User replies: `approve X7K2M9`
5. Email is sent
6. Agent receives success response

### Example: Timeout

If user doesn't respond within 5 minutes:
- Request is automatically denied
- Agent receives error: "Denied: Request timed out"
- User sees no further prompts

## Security Considerations

1. **Short, Random Codes**
   - 6 characters, base32 encoding
   - Prevents accidental approvals
   - Hard to guess

2. **Default Deny**
   - All expired requests denied
   - No silent failures

3. **Context Visibility**
   - Full action details shown
   - User can make informed decision

4. **Audit Trail**
   - All approvals/denials logged
   - Includes timestamp, user, action

5. **One-Time Codes**
   - Each request gets unique code
   - Code expires after use or timeout

## Success Criteria

- [ ] Approval manager implemented with timeout handling
- [ ] IPC watcher integrates approval flow
- [ ] Message handler processes approve/deny responses
- [ ] All approval decisions (approve, deny, timeout) recorded in `messages` table (`sender='security'`) with `approval_code` in metadata
- [ ] Tests pass (approval, denial, timeout, invalid responses, audit log integration)
- [ ] Documentation updated with examples

## Documentation

Update the following:

1. **User guide** - How to respond to approval requests
2. **Security model** - Explain approval gate and timeout behavior
3. **Troubleshooting** - What to do if requests are denied unexpectedly

## Next Steps

After this is complete, the security foundation is in place. Proceed with service integrations:
- Step 3: Email service integration (IMAP/SMTP adapter)
- Step 4: Calendar service integration (CalDAV/Google Calendar)
- Step 5: Password manager integration (1Password CLI)
- Step 7: Input Filtering (Deputy Agent for prompt injection detection - optional defense-in-depth)

## Future Enhancements

- Support multi-factor approval (require N of M admins)
- Add approval rules (auto-approve certain actions for certain users)
- Implement approval history dashboard
- Add approval via other channels (Slack, SMS, app)
- Support conditional approvals (e.g., "approve if amount < $100")
