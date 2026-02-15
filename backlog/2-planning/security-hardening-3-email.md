# Security Hardening: Step 3 - Email Integration

## Overview

Implement the host-side email service adapter that processes email-related IPC requests using real IMAP/SMTP connections.

## Scope

This step adds the email service integration layer on the host, enabling agents to read and send emails through the policy-gated IPC mechanism. Email credentials are stored and used only by the host process - never exposed to the container.

## Dependencies

- ✅ Step 1: Workspace Security Profiles (must be complete)
- ✅ Step 2: MCP Tools & Basic Policy (must be complete)

## Background

Agents can already call `read_email` and `send_email` MCP tools (from Step 2), which write IPC requests. This step implements the host-side handler that:

1. Reads IPC requests from agents
2. Connects to real email services (IMAP/SMTP or Gmail API)
3. Executes the operation using stored credentials
4. Returns results via IPC response

## Implementation Options

Two approaches for email integration:

### Option A: IMAP/SMTP (Universal)
- Works with any email provider
- Uses standard protocols
- Requires app-specific passwords for Gmail/Outlook
- Libraries: `imaplib` (built-in), `smtplib` (built-in)

### Option B: Gmail API (Gmail-only)
- Better security (OAuth2 tokens)
- Richer metadata and features
- Requires Google Cloud project setup
- Library: `google-api-python-client`

**Recommendation:** Start with Option A (IMAP/SMTP) for simplicity and broader compatibility. Can add Option B later as an alternative adapter.

## Implementation

### 1. Email Configuration Schema

**File:** `src/pynchy/config/services.py` (new file)

```python
"""Service configuration types."""

from __future__ import annotations

from typing import Literal, TypedDict


class IMAPConfig(TypedDict):
    """IMAP server configuration."""

    host: str
    port: int
    username: str
    password: str  # TODO: Encrypt or use keyring
    use_ssl: bool


class SMTPConfig(TypedDict):
    """SMTP server configuration."""

    host: str
    port: int
    username: str
    password: str  # TODO: Encrypt or use keyring
    use_tls: bool


class EmailServiceConfig(TypedDict):
    """Email service configuration."""

    provider: Literal["imap_smtp", "gmail_api"]
    imap: IMAPConfig | None
    smtp: SMTPConfig | None
    default_from: str  # Default sender address


class ServicesConfig(TypedDict):
    """All service configurations."""

    email: EmailServiceConfig | None
    # Future: calendar, passwords, etc.
```

### 2. Email Adapter

**File:** `src/pynchy/services/email_adapter.py` (new file)

```python
"""Email service adapter using IMAP/SMTP."""

from __future__ import annotations

import email
import imaplib
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from pynchy.config.services import EmailServiceConfig

logger = logging.getLogger(__name__)


class EmailAdapter:
    """Adapter for email operations via IMAP/SMTP."""

    def __init__(self, config: EmailServiceConfig):
        self.config = config
        if config["provider"] != "imap_smtp":
            raise ValueError(f"Unsupported email provider: {config['provider']}")

        if not config["imap"] or not config["smtp"]:
            raise ValueError("IMAP and SMTP config required for imap_smtp provider")

    def read_emails(
        self,
        folder: str = "INBOX",
        limit: int = 10,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Read emails from mailbox.

        Args:
            folder: IMAP folder name (default: INBOX)
            limit: Maximum number of emails to return
            unread_only: Only return unread emails

        Returns:
            List of email dicts with keys: id, from, subject, date, body_preview
        """
        imap_config = self.config["imap"]

        # Connect to IMAP
        if imap_config["use_ssl"]:
            imap = imaplib.IMAP4_SSL(imap_config["host"], imap_config["port"])
        else:
            imap = imaplib.IMAP4(imap_config["host"], imap_config["port"])

        try:
            # Login
            imap.login(imap_config["username"], imap_config["password"])

            # Select folder
            imap.select(folder, readonly=True)

            # Search for emails
            search_criteria = "UNSEEN" if unread_only else "ALL"
            status, messages = imap.search(None, search_criteria)

            if status != "OK":
                raise Exception(f"IMAP search failed: {status}")

            email_ids = messages[0].split()
            email_ids = email_ids[-limit:]  # Get last N emails

            results = []
            for email_id in reversed(email_ids):  # Newest first
                status, msg_data = imap.fetch(email_id, "(RFC822)")
                if status != "OK":
                    continue

                # Parse email
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Extract fields
                results.append({
                    "id": email_id.decode(),
                    "from": msg.get("From", ""),
                    "subject": msg.get("Subject", ""),
                    "date": msg.get("Date", ""),
                    "body_preview": self._extract_body_preview(msg),
                })

            return results

        finally:
            imap.logout()

    def send_email(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Send an email.

        Args:
            to: Recipient email address(es)
            subject: Email subject
            body: Email body (plain text)
            cc: CC recipients (optional)
            bcc: BCC recipients (optional)

        Returns:
            Dict with status and message_id
        """
        smtp_config = self.config["smtp"]

        # Normalize recipients to lists
        if isinstance(to, str):
            to = [to]
        if isinstance(cc, str):
            cc = [cc]
        elif cc is None:
            cc = []
        if isinstance(bcc, str):
            bcc = [bcc]
        elif bcc is None:
            bcc = []

        # Create message
        msg = MIMEMultipart()
        msg["From"] = self.config["default_from"]
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain"))

        # Connect to SMTP
        if smtp_config["use_tls"]:
            smtp = smtplib.SMTP(smtp_config["host"], smtp_config["port"])
            smtp.starttls()
        else:
            smtp = smtplib.SMTP_SSL(smtp_config["host"], smtp_config["port"])

        try:
            # Login
            smtp.login(smtp_config["username"], smtp_config["password"])

            # Send
            all_recipients = to + cc + bcc
            smtp.send_message(msg, to_addrs=all_recipients)

            return {
                "status": "sent",
                "message_id": msg["Message-ID"],
            }

        finally:
            smtp.quit()

    def _extract_body_preview(self, msg: email.message.Message, max_length: int = 200) -> str:
        """Extract plain text preview from email body."""
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode(errors="ignore")
                    break
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")

        # Clean and truncate
        body = body.strip()
        if len(body) > max_length:
            body = body[:max_length] + "..."

        return body
```

### 3. Register Email Handler in IPC Watcher

**File:** `src/pynchy/ipc/watcher.py`

Update the `_process_allowed_request` method:

```python
from pynchy.services.email_adapter import EmailAdapter

class IPCWatcher:
    def __init__(self, group_config: dict, services_config: dict):
        # ... existing init ...

        # Initialize service adapters
        if services_config.get("email"):
            self.email_adapter = EmailAdapter(services_config["email"])
        else:
            self.email_adapter = None

    async def _process_allowed_request(self, tool_name: str, request: dict, request_id: str):
        """Process an allowed request using appropriate service adapter."""
        try:
            if tool_name == "read_email":
                if not self.email_adapter:
                    raise Exception("Email service not configured")

                result = self.email_adapter.read_emails(
                    folder=request.get("folder", "INBOX"),
                    limit=request.get("limit", 10),
                    unread_only=request.get("unread_only", False),
                )

            elif tool_name == "send_email":
                if not self.email_adapter:
                    raise Exception("Email service not configured")

                result = self.email_adapter.send_email(
                    to=request["to"],
                    subject=request["subject"],
                    body=request["body"],
                    cc=request.get("cc"),
                    bcc=request.get("bcc"),
                )

            else:
                # Other tools (calendar, passwords) - not yet implemented
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

**File:** `config/services.json` (new file, user-created)

```json
{
  "email": {
    "provider": "imap_smtp",
    "imap": {
      "host": "imap.gmail.com",
      "port": 993,
      "username": "user@gmail.com",
      "password": "app-specific-password", // pragma: allowlist secret
      "use_ssl": true
    },
    "smtp": {
      "host": "smtp.gmail.com",
      "port": 587,
      "username": "user@gmail.com",
      "password": "app-specific-password", // pragma: allowlist secret
      "use_tls": true
    },
    "default_from": "user@gmail.com"
  }
}
```

**Security Note:** Credentials should be encrypted or stored in system keyring. For now, plaintext in a file outside the repo (added to `.gitignore`).

## Tests

**File:** `tests/test_email_adapter.py`

```python
"""Tests for email adapter."""

import pytest
from unittest.mock import MagicMock, patch

from pynchy.services.email_adapter import EmailAdapter


@pytest.fixture
def email_config():
    """Sample email config for testing."""
    return {
        "provider": "imap_smtp",
        "imap": {
            "host": "imap.example.com",
            "port": 993,
            "username": "test@example.com",
            "password": "password", // pragma: allowlist secret
            "use_ssl": True,
        },
        "smtp": {
            "host": "smtp.example.com",
            "port": 587,
            "username": "test@example.com",
            "password": "password", // pragma: allowlist secret
            "use_tls": True,
        },
        "default_from": "test@example.com",
    }


def test_email_adapter_init(email_config):
    """Test email adapter initialization."""
    adapter = EmailAdapter(email_config)
    assert adapter.config == email_config


def test_email_adapter_invalid_provider():
    """Test email adapter rejects invalid provider."""
    config = {"provider": "invalid", "imap": None, "smtp": None, "default_from": "test@example.com"}
    with pytest.raises(ValueError, match="Unsupported email provider"):
        EmailAdapter(config)


@patch("imaplib.IMAP4_SSL")
def test_read_emails(mock_imap, email_config):
    """Test reading emails via IMAP."""
    # Mock IMAP connection
    mock_conn = MagicMock()
    mock_imap.return_value = mock_conn

    mock_conn.search.return_value = ("OK", [b"1 2 3"])
    mock_conn.fetch.return_value = ("OK", [(None, b"From: sender@example.com\nSubject: Test\n\nBody")])

    adapter = EmailAdapter(email_config)
    emails = adapter.read_emails()

    assert len(emails) == 3
    mock_conn.login.assert_called_once()
    mock_conn.logout.assert_called_once()


@patch("smtplib.SMTP")
def test_send_email(mock_smtp, email_config):
    """Test sending email via SMTP."""
    # Mock SMTP connection
    mock_conn = MagicMock()
    mock_smtp.return_value = mock_conn

    adapter = EmailAdapter(email_config)
    result = adapter.send_email(
        to="recipient@example.com",
        subject="Test",
        body="Test body"
    )

    assert result["status"] == "sent"
    mock_conn.login.assert_called_once()
    mock_conn.send_message.assert_called_once()
    mock_conn.quit.assert_called_once()
```

## Security Considerations

1. **Credential Storage**
   - Current: Plaintext in config file (not committed to git)
   - Future: Use system keyring (e.g., `keyring` library) or environment variables
   - Never expose credentials to container

2. **TLS/SSL**
   - Always use SSL for IMAP (port 993)
   - Always use TLS for SMTP (port 587 with STARTTLS)
   - Validate certificates

3. **Rate Limiting**
   - Add rate limits to prevent abuse
   - Track email sends per hour/day per workspace

4. **Input Validation**
   - Validate email addresses (regex or `email-validator` library)
   - Sanitize email bodies (strip scripts, validate attachments)
   - Limit email size and recipient count

## Success Criteria

- [ ] Email adapter implemented with IMAP/SMTP support
- [ ] IPC watcher integrates email adapter
- [ ] Configuration schema defined and documented
- [ ] Tests pass (adapter creation, mocked IMAP/SMTP calls)
- [ ] Credentials stored securely (outside repo, in `.gitignore`)
- [ ] Documentation updated with setup instructions

## Documentation

Update the following:

1. **Setup guide** - How to configure email service (app passwords for Gmail)
2. **Email tools reference** - Examples of reading/sending emails
3. **Security notes** - Credential storage, TLS requirements

## Gmail Setup Instructions

For users with Gmail:

1. Enable 2-factor authentication on Google account
2. Generate app-specific password: https://myaccount.google.com/apppasswords
3. Use app password in `config/services.json`
4. IMAP host: `imap.gmail.com`, port 993
5. SMTP host: `smtp.gmail.com`, port 587

## Next Steps

After this is complete:
- Step 4: Calendar service integration (CalDAV/Google Calendar)
- Step 5: Password manager integration (1Password CLI)
- Step 6: Human approval gate (for send_email, etc.)

## Future Enhancements

- Support Gmail API as alternative provider
- Support OAuth2 for better security
- Add attachment support
- Add HTML email support
- Implement email search and filtering
- Add draft management
