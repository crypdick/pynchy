"""SMTP transport adapter for the local Proton Mail Bridge listener."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

_BRIDGE_HOST = "127.0.0.1"
_SMTP_TIMEOUT_SECONDS = 30


class ProtonBridgeSmtpError(RuntimeError):
    """The local Bridge SMTP listener rejected a transport operation."""


@runtime_checkable
class _SmtpConfiguration(Protocol):
    """Connection fields the SMTP adapter needs from Bridge configuration."""

    username: str
    smtp_port: int


@runtime_checkable
class SmtpConnection(Protocol):
    """The small SMTP operation surface used by the Bridge client."""

    def send_message(
        self,
        message: EmailMessage,
        *,
        sender: str,
        recipients: list[str],
    ) -> None: ...

    def quit(self) -> None: ...


type SmtpConnectionFactory = Callable[[_SmtpConfiguration, SecretStr], SmtpConnection]


@dataclass
class BridgeSmtpConnection:
    """Typed adapter around ``smtplib.SMTP``'s flexible send result."""

    client: smtplib.SMTP

    def send_message(
        self,
        message: EmailMessage,
        *,
        sender: str,
        recipients: list[str],
    ) -> None:
        try:
            refused = self.client.send_message(message, from_addr=sender, to_addrs=recipients)
        except smtplib.SMTPException as exc:
            raise ProtonBridgeSmtpError("Proton Bridge SMTP request failed") from exc
        if refused:
            raise ProtonBridgeSmtpError("Proton Bridge SMTP rejected a recipient")

    def quit(self) -> None:
        try:
            self.client.quit()
        except (OSError, smtplib.SMTPException) as exc:
            raise ProtonBridgeSmtpError("Proton Bridge SMTP shutdown failed") from exc


def open_bridge_smtp_connection(
    configuration: _SmtpConfiguration,
    password: SecretStr,
) -> SmtpConnection:
    """Authenticate to Bridge's loopback-only SMTP listener with its TLS certificate."""
    client = _open_smtp_client(configuration.smtp_port)
    _authenticate_smtp_client(client, configuration.username, password)
    return BridgeSmtpConnection(client)


def _open_smtp_client(port: int) -> smtplib.SMTP:
    try:
        return smtplib.SMTP(_BRIDGE_HOST, port, timeout=_SMTP_TIMEOUT_SECONDS)
    except (OSError, smtplib.SMTPException) as exc:
        raise ProtonBridgeSmtpError("Proton Bridge SMTP request failed") from exc


def _authenticate_smtp_client(client: smtplib.SMTP, username: str, password: SecretStr) -> None:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        client.login(username, password.get_secret_value())
    except (OSError, smtplib.SMTPException) as exc:
        raise ProtonBridgeSmtpError("Proton Bridge SMTP request failed") from exc
