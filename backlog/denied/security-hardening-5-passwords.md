# Security Hardening: Step 5 - Password Manager Integration

## Overview

Implement the host-side password manager adapter that processes password-related IPC requests using 1Password CLI.

## Scope

This step adds password manager integration, enabling agents to search for and retrieve passwords through the policy-gated IPC mechanism. Passwords are NEVER stored in the agent or container - they're fetched on-demand from 1Password and immediately returned via IPC.

## Dependencies

- ✅ Step 0: Reduce IPC Surface (must be complete — narrows IPC before adding tools)
- ✅ Step 1: Workspace Security Profiles (must be complete)
- ✅ Step 2: MCP Tools & Basic Policy (must be complete)
- ✅ Step 6: Human Approval Gate (**required** — `get_password` must require approval)

## Background

Agents can already call `search_passwords` and `get_password` MCP tools (from Step 2), which write IPC requests. This step implements the host-side handler that executes these operations using the 1Password CLI.

## Why 1Password CLI?

- **Security**: Credentials never touch the agent or container
- **Audit trail**: All access logged by 1Password
- **Biometric unlock**: Can require Touch ID/Face ID for sensitive operations
- **Mature CLI**: Official 1Password CLI (`op`) is well-maintained and feature-rich

Alternative password managers (Bitwarden, pass, etc.) can be added later following the same pattern.

## Implementation

### 1. Password Manager Configuration Schema

**File:** `src/pynchy/config/services.py`

Extend the existing `ServicesConfig`:

```python
from typing import Literal


class OnePasswordConfig(TypedDict):
    """1Password CLI configuration."""

    account: str  # Account shorthand or URL
    vault: str | None  # Default vault (None = all vaults)


class PasswordServiceConfig(TypedDict):
    """Password manager service configuration."""

    provider: Literal["1password"]
    onepassword: OnePasswordConfig | None


class ServicesConfig(TypedDict):
    """All service configurations."""

    email: EmailServiceConfig | None
    calendar: CalendarServiceConfig | None
    passwords: PasswordServiceConfig | None  # New field
```

### 2. Password Manager Adapter

**File:** `src/pynchy/services/password_adapter.py` (new file)

```python
"""Password manager adapter using 1Password CLI."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from pynchy.config.services import PasswordServiceConfig

logger = logging.getLogger(__name__)


class PasswordAdapter:
    """Adapter for password operations via 1Password CLI."""

    def __init__(self, config: PasswordServiceConfig):
        self.config = config
        if config["provider"] != "1password":
            raise ValueError(f"Unsupported password provider: {config['provider']}")

        if not config["onepassword"]:
            raise ValueError("1Password config required for 1password provider")

        self.op_config = config["onepassword"]
        self._verify_cli_available()

    def _verify_cli_available(self):
        """Verify 1Password CLI is installed and authenticated."""
        try:
            result = subprocess.run(
                ["op", "account", "get"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0:
                raise Exception(
                    "1Password CLI not authenticated. Run: op account add"
                )

        except FileNotFoundError:
            raise Exception(
                "1Password CLI not found. Install from: https://1password.com/downloads/command-line/"
            )

    def search_passwords(self, query: str) -> list[dict[str, Any]]:
        """Search password vault for items matching query.

        Returns metadata only (titles, URLs, tags) - NOT passwords.

        Args:
            query: Search query string

        Returns:
            List of item dicts with keys: id, title, url, tags, vault
        """
        cmd = [
            "op",
            "item",
            "list",
            "--format=json",
            f"--account={self.op_config['account']}",
        ]

        if self.op_config.get("vault"):
            cmd.append(f"--vault={self.op_config['vault']}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )

            items = json.loads(result.stdout)

            # Filter by query (case-insensitive title match)
            query_lower = query.lower()
            filtered = [
                item for item in items
                if query_lower in item.get("title", "").lower()
            ]

            # Return metadata only
            return [
                {
                    "id": item["id"],
                    "title": item.get("title", ""),
                    "url": item.get("urls", [{}])[0].get("href", "") if item.get("urls") else "",
                    "tags": item.get("tags", []),
                    "vault": item.get("vault", {}).get("name", ""),
                }
                for item in filtered
            ]

        except subprocess.CalledProcessError as e:
            logger.error(f"1Password CLI error: {e.stderr}")
            raise Exception(f"Failed to search passwords: {e.stderr}")

    def get_password(
        self,
        item_id: str,
        field: str = "password",
    ) -> dict[str, Any]:
        """Get password or other field from 1Password item.

        **SECURITY:** This should ALWAYS require human approval (Step 6).

        Args:
            item_id: 1Password item ID
            field: Field to retrieve (default: "password")

        Returns:
            Dict with item metadata and requested field value
        """
        # First, get item metadata (without secrets)
        cmd_get = [
            "op",
            "item",
            "get",
            item_id,
            "--format=json",
            f"--account={self.op_config['account']}",
        ]

        try:
            result = subprocess.run(
                cmd_get,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )

            item = json.loads(result.stdout)

        except subprocess.CalledProcessError as e:
            logger.error(f"1Password CLI error: {e.stderr}")
            raise Exception(f"Failed to get item metadata: {e.stderr}")

        # Now get the specific field value
        cmd_field = [
            "op",
            "item",
            "get",
            item_id,
            f"--field={field}",
            f"--account={self.op_config['account']}",
        ]

        try:
            result = subprocess.run(
                cmd_field,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )

            field_value = result.stdout.strip()

        except subprocess.CalledProcessError as e:
            logger.error(f"1Password CLI error: {e.stderr}")
            raise Exception(f"Failed to get field '{field}': {e.stderr}")

        return {
            "item_id": item_id,
            "title": item.get("title", ""),
            "url": item.get("urls", [{}])[0].get("href", "") if item.get("urls") else "",
            "field": field,
            "value": field_value,
        }
```

### 3. Register Password Handler in IPC Watcher

**File:** `src/pynchy/ipc/watcher.py`

Update the initialization and request processing:

```python
from pynchy.services.password_adapter import PasswordAdapter

class IPCWatcher:
    def __init__(self, group_config: dict, services_config: dict):
        # ... existing init ...

        # Initialize password adapter
        if services_config.get("passwords"):
            self.password_adapter = PasswordAdapter(services_config["passwords"])
        else:
            self.password_adapter = None

    async def _process_allowed_request(self, tool_name: str, request: dict, request_id: str):
        """Process an allowed request using appropriate service adapter."""
        try:
            # ... existing email and calendar handlers ...

            if tool_name == "search_passwords":
                if not self.password_adapter:
                    raise Exception("Password manager not configured")

                result = self.password_adapter.search_passwords(
                    query=request["query"],
                )

            elif tool_name == "get_password":
                if not self.password_adapter:
                    raise Exception("Password manager not configured")

                result = self.password_adapter.get_password(
                    item_id=request["item_id"],
                    field=request.get("field", "password"),
                )

            else:
                result = f"Handler for {tool_name} not yet implemented"

            # Send success response
            response_file = Path(f"/workspace/ipc/input/{request_id}_response.json")
            with open(response_file, "w") as f:
                json.dump({"result": result}, f)

        except Exception as e:
            logger.error(f"Error processing {tool_name}: {e}")
            await self._send_error_response(request_id, str(e))
```

### 4. Configuration Example

**File:** `config/services.json`

Add password configuration:

```json
{
  "email": { /* ... */ },
  "calendar": { /* ... */ },
  "passwords": {
    "provider": "1password",
    "onepassword": {
      "account": "my.1password.com",
      "vault": "Personal"
    }
  }
}
```

## Setup Requirements

### Prerequisites

1. **Install 1Password CLI**
   - macOS: `brew install --cask 1password-cli`
   - Linux: Download from https://1password.com/downloads/command-line/
   - Windows: Download from same URL

2. **Authenticate CLI**
   ```bash
   op account add
   ```
   Follow prompts to sign in. This creates a session that persists.

3. **Test Authentication**
   ```bash
   op account get
   op vault list
   ```

### Security Model

- CLI sessions are tied to the host user account
- Can be configured to require biometric auth (Touch ID) for each access
- All access is logged in 1Password activity log
- Credentials never persist in logs or agent memory

## Tests

**File:** `tests/test_password_adapter.py`

```python
"""Tests for password adapter."""

import json
import pytest
from unittest.mock import MagicMock, patch

from pynchy.services.password_adapter import PasswordAdapter


@pytest.fixture
def password_config():
    """Sample password config for testing."""
    return {
        "provider": "1password",
        "onepassword": {
            "account": "my.1password.com",
            "vault": "Test Vault",
        },
    }


def test_password_adapter_init(password_config):
    """Test password adapter initialization."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        adapter = PasswordAdapter(password_config)
        assert adapter.config == password_config


def test_password_adapter_invalid_provider():
    """Test password adapter rejects invalid provider."""
    config = {"provider": "invalid", "onepassword": None}
    with pytest.raises(ValueError, match="Unsupported password provider"):
        PasswordAdapter(config)


def test_password_adapter_cli_not_found():
    """Test error when 1Password CLI not installed."""
    config = {
        "provider": "1password",
        "onepassword": {"account": "test", "vault": None},
    }

    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(Exception, match="1Password CLI not found"):
            PasswordAdapter(config)


def test_password_adapter_not_authenticated():
    """Test error when CLI not authenticated."""
    config = {
        "provider": "1password",
        "onepassword": {"account": "test", "vault": None},
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="not signed in")
        with pytest.raises(Exception, match="not authenticated"):
            PasswordAdapter(config)


@patch("subprocess.run")
def test_search_passwords(mock_run, password_config):
    """Test searching passwords."""
    # Mock CLI response
    mock_items = [
        {
            "id": "item-123",
            "title": "GitHub",
            "urls": [{"href": "https://github.com"}],
            "tags": ["dev"],
            "vault": {"name": "Test Vault"},
        },
        {
            "id": "item-456",
            "title": "Gmail",
            "urls": [{"href": "https://gmail.com"}],
            "tags": ["email"],
            "vault": {"name": "Test Vault"},
        },
    ]

    mock_run.side_effect = [
        MagicMock(returncode=0),  # account get (init)
        MagicMock(returncode=0, stdout=json.dumps(mock_items)),  # item list
    ]

    adapter = PasswordAdapter(password_config)
    results = adapter.search_passwords("git")

    assert len(results) == 1
    assert results[0]["title"] == "GitHub"
    assert results[0]["url"] == "https://github.com"


@patch("subprocess.run")
def test_get_password(mock_run, password_config):
    """Test getting password."""
    # Mock CLI responses
    mock_item = {
        "id": "item-123",
        "title": "GitHub",
        "urls": [{"href": "https://github.com"}],
    }

    mock_run.side_effect = [
        MagicMock(returncode=0),  # account get (init)
        MagicMock(returncode=0, stdout=json.dumps(mock_item)),  # item get (metadata)
        MagicMock(returncode=0, stdout="super-secret-password"),  # item get (field)
    ]

    adapter = PasswordAdapter(password_config)
    result = adapter.get_password("item-123")

    assert result["item_id"] == "item-123"
    assert result["title"] == "GitHub"
    assert result["value"] == "super-secret-password"
    assert result["field"] == "password"
```

## Security Considerations

1. **ALWAYS Require Approval**
   - `get_password` accesses sensitive_info=true data. When the container is tainted (has read from an untrusted source), human approval is required.
   - `search_passwords` returns metadata only. Since passwords service has trusted_source=true, this doesn't taint the container.
   - Step 6 (Human Approval Gate) is critical for this feature

2. **No Credential Persistence**
   - Passwords returned via IPC, never logged
   - Agent memory should not persist password values
   - Consider clearing IPC response files immediately after read

3. **Biometric Authentication**
   - Configure 1Password to require Touch ID for CLI access
   - Settings → Developer → "Require Touch ID for CLI"

4. **Audit Logging**
   - All `get_password` calls logged in 1Password activity log
   - Review periodically for suspicious access

5. **Rate Limiting**
   - Limit password retrievals per hour/day
   - Alert on excessive password access

## ServiceTrustConfig Example

```toml
[services.passwords]
trusted_source = true    # vault metadata is trusted
sensitive_info = true     # passwords are secrets
trusted_sink = false      # retrieving passwords is sensitive
```

## Success Criteria

- [ ] Password adapter implemented with 1Password CLI support
- [ ] IPC watcher integrates password adapter
- [ ] Configuration schema extended
- [ ] Tests pass (adapter creation, mocked CLI calls)
- [ ] Documentation updated with setup instructions
- [ ] Security profile examples include password tools

## Documentation

Update the following:

1. **Setup guide** - How to install and authenticate 1Password CLI
2. **Password tools reference** - Examples of searching/retrieving passwords
3. **Security best practices** - Biometric auth, approval requirements, audit logs

## 1Password CLI Setup Guide

### Installation

**macOS:**
```bash
brew install --cask 1password-cli
```

**Linux:**
```bash
curl -sS https://downloads.1password.com/linux/keys/1password.asc | \
  sudo gpg --dearmor --output /usr/share/keyrings/1password-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/amd64 stable main" | \
  sudo tee /etc/apt/sources.list.d/1password.list
sudo apt update && sudo apt install 1password-cli
```

### Authentication

1. Add account:
   ```bash
   op account add
   ```

2. Verify:
   ```bash
   op account get
   op vault list
   ```

3. Enable biometric (recommended):
   - Open 1Password app
   - Settings → Developer
   - Enable "Integrate with 1Password CLI"
   - Enable "Require Touch ID for CLI"

## Next Steps

After this is complete:
- **Step 6: Human Approval Gate** (CRITICAL - required for `get_password` to be safe)
- Step 7: Input Filtering (Deputy Agent for prompt injection detection)

## Future Enhancements

- Support other password managers (Bitwarden, pass, LastPass)
- Add TOTP/2FA code retrieval
- Implement secure notes access
- Add password generation via CLI
- Support password updates
