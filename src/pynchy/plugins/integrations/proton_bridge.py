"""Direct, read-only IMAP access to a local Proton Mail Bridge instance."""

from __future__ import annotations

import base64
import binascii
import imaplib
import os
import re
import shlex
import ssl
import subprocess  # noqa: S404 - runs one administrator-configured credential command.
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from email import policy
from email.message import (
    EmailMessage,  # noqa: TC003, RUF100 - beartype resolves this hint at runtime.
)
from email.parser import BytesParser
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

_BRIDGE_HOST = "127.0.0.1"
_BRIDGE_IMAP_PORT = 1143
# This is an environment-variable name, not a password value.
_PASSWORD_COMMAND_ENV = "PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND"  # noqa: S105  # pragma: allowlist secret
_USERNAME_ENV = "PYNCHY_PROTON_BRIDGE_USERNAME"
_COMMAND_TIMEOUT_SECONDS = 15
_IMAP_TIMEOUT_SECONDS = 30
_MAILBOX_NAME_PATTERN = re.compile(rb'(?:NIL|"(?:[^"\\]|\\.)*"|[^ ]+)$')
_FLAGS_PATTERN = re.compile(rb"FLAGS \((?P<flags>[^)]*)\)")


class ProtonMailError(RuntimeError):
    """Raised when the local Proton Bridge integration cannot complete an operation."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProtonMailbox(_StrictModel):
    """A mailbox's display name and exact IMAP identifier."""

    name: str
    mailbox: str


class ProtonMailboxList(_StrictModel):
    """Mailbox list response returned to the MCP layer."""

    mailboxes: list[ProtonMailbox]


class ProtonMessageEnvelope(_StrictModel):
    """Message metadata that can safely identify a later read request."""

    message_id: str | None
    sender: str | None
    subject: str | None
    date: str | None
    seen: bool


class ProtonMailList(_StrictModel):
    """Message listing returned to the MCP layer."""

    messages: list[ProtonMessageEnvelope]


class ProtonMailHeader(_StrictModel):
    """One MIME header, preserving repeated header names."""

    name: str
    value: str


class ProtonMessage(_StrictModel):
    """Parsed plaintext email content returned to the MCP layer."""

    message_id: str
    body: str
    headers: list[ProtonMailHeader] | None = None


class ProtonBridgeConfiguration(_StrictModel):
    """Connection and credential-command configuration for local Bridge IMAP."""

    username: str = Field(min_length=1)
    password_command: str = Field(min_length=1)

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("username must be a single non-empty line")
        return normalized

    @classmethod
    def from_environment(cls) -> ProtonBridgeConfiguration:
        """Load the deliberately narrow Bridge configuration from the MCP environment."""
        try:
            return cls.model_validate(
                {
                    "username": os.environ.get(_USERNAME_ENV),
                    "password_command": os.environ.get(_PASSWORD_COMMAND_ENV),
                }
            )
        except ValidationError as exc:
            raise ProtonMailError(
                f"Configure {_USERNAME_ENV} and {_PASSWORD_COMMAND_ENV} for Proton Bridge"
            ) from exc


@runtime_checkable
class ProtonMailClient(Protocol):
    """Read-only operations required by the Proton Mail MCP server."""

    def list_mailboxes(self) -> ProtonMailboxList: ...

    def list_mail(
        self,
        *,
        mailbox: str,
        limit: int,
        offset: int,
        unread: bool,
    ) -> ProtonMailList: ...

    def read_mail(
        self,
        *,
        mailbox: str,
        message_id: str,
        include_headers: bool,
    ) -> ProtonMessage: ...


@runtime_checkable
class _ImapConnection(Protocol):
    """The small subset of ``imaplib.IMAP4`` used by the Bridge client."""

    def list_mailboxes(self) -> tuple[str, list[bytes | None]]: ...

    def logout(self) -> tuple[str, list[bytes]]: ...

    def select(
        self,
        mailbox: str,
        readonly: bool = False,  # noqa: FBT001, FBT002 - mirrors imaplib's protocol API.
    ) -> tuple[str, list[bytes]]: ...

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]: ...


@runtime_checkable
class PasswordProvider(Protocol):
    """Read the Bridge app password without putting it in source or MCP config."""

    def get_password(self) -> SecretStr: ...


@dataclass(frozen=True)
class CommandPasswordProvider:
    """Obtain the Bridge app password from an administrator-configured command."""

    command: str

    def get_password(self) -> SecretStr:
        try:
            args = shlex.split(self.command)
        except ValueError as exc:
            raise ProtonMailError("Proton Bridge password command is not valid") from exc
        if not args:
            raise ProtonMailError("Proton Bridge password command is empty")

        try:
            process = subprocess.run(  # noqa: S603, RUF100 - command is trusted host config.
                args,
                capture_output=True,
                check=False,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtonMailError("Could not retrieve the Proton Bridge app password") from exc
        if process.returncode != 0:
            raise ProtonMailError("Could not retrieve the Proton Bridge app password")

        password = process.stdout.strip()
        if not password:
            raise ProtonMailError("Proton Bridge password command returned no password")
        return SecretStr(password)


ImapConnectionFactory = Callable[[ProtonBridgeConfiguration, SecretStr], _ImapConnection]


@dataclass
class _BridgeImapConnection:
    """Typed adapter around imaplib's loosely typed protocol responses."""

    client: imaplib.IMAP4

    def list_mailboxes(self) -> tuple[str, list[bytes | None]]:
        status, data = self.client.list()
        return status, [item for item in data if isinstance(item, bytes)]

    def logout(self) -> tuple[str, list[bytes]]:
        status, data = self.client.logout()
        return status, [item for item in data if isinstance(item, bytes)]

    def select(
        self,
        mailbox: str,
        readonly: bool = False,  # noqa: FBT001, FBT002 - mirrors imaplib's protocol API.
    ) -> tuple[str, list[bytes]]:
        status, data = self.client.select(mailbox, readonly=readonly)
        return status, [item for item in data if isinstance(item, bytes)]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        # imaplib supports None as SEARCH's charset despite its narrow type stub.
        return self.client.uid(command, *args)  # type: ignore[arg-type]


class ProtonBridgeImapClient:
    """Protocol client that keeps every IMAP operation read-only and single-session."""

    def __init__(
        self,
        configuration: ProtonBridgeConfiguration,
        password_provider: PasswordProvider,
        connection_factory: ImapConnectionFactory | None = None,
    ) -> None:
        self._configuration = configuration
        self._password_provider = password_provider
        self._connection_factory = connection_factory or _open_bridge_connection

    def list_mailboxes(self) -> ProtonMailboxList:
        with self._connection() as connection:
            status, data = connection.list_mailboxes()
            _require_ok(status, "list mailboxes")
            return ProtonMailboxList(
                mailboxes=[_parse_mailbox(response) for response in data if response is not None]
            )

    def list_mail(
        self,
        *,
        mailbox: str,
        limit: int,
        offset: int,
        unread: bool,
    ) -> ProtonMailList:
        with self._connection() as connection:
            _select_readonly(connection, mailbox)
            uids = _search_uids(connection, "UNSEEN" if unread else "ALL")
            requested_uids = list(reversed(uids))[offset : offset + limit]
            return ProtonMailList(
                messages=[_message_envelope(connection, uid) for uid in requested_uids]
            )

    def read_mail(self, *, mailbox: str, message_id: str, include_headers: bool) -> ProtonMessage:
        with self._connection() as connection:
            _select_readonly(connection, mailbox)
            uid = _find_message_uid(connection, message_id)
            _metadata, raw_message = _fetch_message(connection, uid, "BODY.PEEK[]")

        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        parsed_message_id = message.get("Message-ID")
        if parsed_message_id is None:
            raise ProtonMailError("Proton Bridge returned a message without a Message-ID")
        headers = _message_headers(message) if include_headers else None
        return ProtonMessage(
            message_id=parsed_message_id,
            body=_message_body(message),
            headers=headers,
        )

    @contextmanager
    def _connection(self) -> Iterator[_ImapConnection]:
        connection: _ImapConnection | None = None
        try:
            connection = self._connection_factory(
                self._configuration,
                self._password_provider.get_password(),
            )
            yield connection
        except ProtonMailError:
            raise
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ProtonMailError("Proton Bridge IMAP request failed") from exc
        finally:
            if connection is not None:
                with suppress(OSError, imaplib.IMAP4.error):
                    connection.logout()


def create_proton_mail_client() -> ProtonMailClient:
    """Create the production direct-IMAP client from the MCP process environment."""
    configuration = ProtonBridgeConfiguration.from_environment()
    return ProtonBridgeImapClient(
        configuration=configuration,
        password_provider=CommandPasswordProvider(configuration.password_command),
    )


def _open_bridge_connection(
    configuration: ProtonBridgeConfiguration,
    password: SecretStr,
) -> _ImapConnection:
    """Authenticate to the loopback-only Bridge listener with its self-signed TLS cert."""
    client = imaplib.IMAP4(_BRIDGE_HOST, _BRIDGE_IMAP_PORT, timeout=_IMAP_TIMEOUT_SECONDS)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    client.starttls(ssl_context=context)
    client.login(configuration.username, password.get_secret_value())
    return _BridgeImapConnection(client)


def _select_readonly(connection: _ImapConnection, mailbox: str) -> None:
    status, _data = connection.select(_imap_quote(mailbox), readonly=True)
    _require_ok(status, f"select mailbox {mailbox!r}")


def _search_uids(connection: _ImapConnection, criterion: str) -> list[str]:
    status, data = connection.uid("SEARCH", None, criterion)
    _require_ok(status, "search mailbox")
    if not data or not isinstance(data[0], bytes):
        return []
    return [uid.decode("ascii") for uid in data[0].split() if uid.isdigit()]


def _find_message_uid(connection: _ImapConnection, message_id: str) -> str:
    status, data = connection.uid(
        "SEARCH",
        None,
        "HEADER",
        "Message-ID",
        _imap_quote(message_id),
    )
    _require_ok(status, "find message by Message-ID")
    if not data or not isinstance(data[0], bytes):
        raise ProtonMailError("Message was not found in the selected mailbox")
    matches = [uid.decode("ascii") for uid in data[0].split() if uid.isdigit()]
    if not matches:
        raise ProtonMailError("Message was not found in the selected mailbox")
    if len(matches) > 1:
        raise ProtonMailError("Message-ID is ambiguous in the selected mailbox")
    return matches[0]


def _message_envelope(connection: _ImapConnection, uid: str) -> ProtonMessageEnvelope:
    metadata, raw_headers = _fetch_message(connection, uid, "BODY.PEEK[HEADER]")
    message = BytesParser(policy=policy.default).parsebytes(raw_headers)
    return ProtonMessageEnvelope(
        message_id=message.get("Message-ID"),
        sender=message.get("From"),
        subject=message.get("Subject"),
        date=message.get("Date"),
        seen=b"\\Seen" in _flags(metadata),
    )


def _fetch_message(connection: _ImapConnection, uid: str, message_part: str) -> tuple[bytes, bytes]:
    status, data = connection.uid("FETCH", uid, f"(UID FLAGS {message_part})")
    _require_ok(status, "fetch message")
    for item in data:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        metadata, payload = item
        if isinstance(metadata, bytes) and isinstance(payload, bytes):
            return metadata, payload
    raise ProtonMailError("Proton Bridge returned an unexpected message response")


def _parse_mailbox(response: bytes) -> ProtonMailbox:
    """Parse a LIST response without changing the identifier needed by SELECT."""
    match = _MAILBOX_NAME_PATTERN.search(response)
    if match is None:
        raise ProtonMailError("Proton Bridge returned an invalid mailbox response")
    mailbox = match.group(0)
    if mailbox == b"NIL":
        raise ProtonMailError("Proton Bridge returned a mailbox without a name")
    if mailbox.startswith(b'"') and mailbox.endswith(b'"'):
        mailbox = mailbox[1:-1].replace(b"\\\\", b"\\").replace(b'\\"', b'"')
    try:
        identifier = mailbox.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtonMailError("Proton Bridge returned an invalid mailbox identifier") from exc
    return ProtonMailbox(name=_decode_modified_utf7(identifier), mailbox=identifier)


def _decode_modified_utf7(value: str) -> str:
    """Decode IMAP's modified UTF-7 mailbox encoding for display only."""
    decoded: list[str] = []
    position = 0
    while position < len(value):
        ampersand = value.find("&", position)
        if ampersand == -1:
            decoded.append(value[position:])
            break
        decoded.append(value[position:ampersand])
        terminator = value.find("-", ampersand)
        if terminator == -1:
            raise ProtonMailError("Proton Bridge returned an invalid international mailbox name")
        encoded = value[ampersand + 1 : terminator]
        if not encoded:
            decoded.append("&")
        else:
            try:
                padding = "=" * (-len(encoded) % 4)
                utf16_bytes = base64.b64decode(
                    (encoded.replace(",", "/") + padding).encode("ascii"), validate=True
                )
                decoded.append(utf16_bytes.decode("utf-16-be"))
            except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                raise ProtonMailError(
                    "Proton Bridge returned an invalid international mailbox name"
                ) from exc
        position = terminator + 1
    return "".join(decoded)


def _flags(metadata: bytes) -> bytes:
    match = _FLAGS_PATTERN.search(metadata)
    return match.group("flags") if match is not None else b""


def _message_headers(message: EmailMessage) -> list[ProtonMailHeader]:
    return [ProtonMailHeader(name=name, value=value) for name, value in message.items()]


def _message_body(message: EmailMessage) -> str:
    preferred_part = message.get_body(preferencelist=("plain", "html"))
    part = preferred_part or message
    content = part.get_content()
    return content if isinstance(content, str) else ""


def _imap_quote(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ProtonMailError("IMAP values must be a single line")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _require_ok(status: str, operation: str) -> None:
    if status != "OK":
        raise ProtonMailError(f"Proton Bridge could not {operation}")
