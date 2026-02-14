# Security Hardening: Step 2 - MCP Tools & Basic Policy

## Overview

Implement new MCP tools for external services (email, calendar, passwords) and add basic policy checking middleware to gate tool execution based on workspace security profiles.

## Scope

This step extends the container's MCP server with new IPC tools and adds the policy enforcement layer that evaluates tool calls against workspace security profiles. Does NOT implement actual service integrations yet - tools will write IPC requests that later steps will process.

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

### 2. Create Policy Middleware

**File:** `src/pynchy/policy/middleware.py` (new file)

```python
"""Policy enforcement middleware for IPC requests."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pynchy.types.security import RiskTier, WorkspaceSecurityProfile

logger = logging.getLogger(__name__)


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


class PolicyMiddleware:
    """Evaluates IPC requests against workspace security profile."""

    def __init__(self, security_profile: WorkspaceSecurityProfile):
        self.security_profile = security_profile

    def evaluate(self, tool_name: str, request: dict) -> PolicyDecision:
        """Evaluate whether tool call should be allowed.

        Args:
            tool_name: Name of the MCP tool being called
            request: The IPC request payload

        Returns:
            PolicyDecision with allowed/denied status and reason
        """
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

### 3. Integrate Policy Middleware into IPC Watcher

**File:** `src/pynchy/ipc/watcher.py` (or wherever IPC files are processed)

```python
from pynchy.policy.middleware import PolicyMiddleware, PolicyDecision

class IPCWatcher:
    def __init__(self, group_config: dict):
        self.group_config = group_config

        # Initialize policy middleware
        security_profile = group_config.get("security_profile")
        if security_profile:
            self.policy = PolicyMiddleware(security_profile)
        else:
            self.policy = None

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
                    # Send for human approval
                    await self._request_approval(tool_name, request, request_id)
                    return  # Approval handler will send response later
                else:
                    # Denied by policy
                    await self._send_error_response(
                        request_id, f"Policy denied: {decision.reason}"
                    )
                    return

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

## Tests

**File:** `tests/test_policy_middleware.py`

```python
"""Tests for policy middleware."""

import pytest

from pynchy.policy.middleware import PolicyMiddleware
from pynchy.types.security import RiskTier


def test_policy_read_only_auto_approved():
    """Test read-only tools are auto-approved."""
    profile = {
        "tools": {
            "read_email": {"tier": RiskTier.READ_ONLY, "enabled": True}
        },
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": False,
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
    }

    policy = PolicyMiddleware(profile)
    decision = policy.evaluate("create_event", {})

    assert decision.allowed is True  # Auto-approved until rules engine added
    assert decision.requires_approval is False
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
- [ ] Policy middleware created with tier-based evaluation
- [ ] IPC watcher integrated with policy enforcement
- [ ] Tests pass (policy logic, tool creation, integration)
- [ ] Mock responses work for testing
- [ ] Documentation updated with new tool schemas

## Documentation

Update the following:

1. **IPC protocol docs** - Document new request types and payloads
2. **MCP tools reference** - Add new tools with examples
3. **Security enforcement** - Explain how policy middleware works

## Notes

- This step does NOT implement actual service integrations (email, calendar, etc.)
- Tools write IPC requests that return mock responses
- Steps 3-5 will implement real service handlers
- Step 6 will implement human approval flow
- Rules engine (for write-tier tools) is placeholder - can be enhanced later

## Next Steps

After this is complete:
- Step 3: Email service integration (IMAP/SMTP adapter)
- Step 4: Calendar service integration (CalDAV/Google Calendar)
- Step 5: Password manager integration (1Password CLI)
- Step 6: Human approval gate (WhatsApp approval flow)
