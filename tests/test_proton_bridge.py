"""Tests for direct Proton Bridge IMAP transport semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from pydantic import SecretStr

from pynchy.plugins.integrations.proton_bridge import (
    CommandPasswordProvider,
    ProtonBridgeConfiguration,
    ProtonBridgeImapClient,
    ProtonMailError,
)

if TYPE_CHECKING:
    from email.message import EmailMessage


def _password_reader() -> SecretStr:
    """Deterministic credential reader for direct-IMAP tests."""
    return SecretStr("test-bridge-password")


@dataclass
class FakeImapConnection:
    """Small programmable IMAP transport that records all protocol operations."""

    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    mailbox_responses: list[bytes] = field(
        default_factory=lambda: [b'(\\HasNoChildren) "/" "INBOX"']
    )
    search_results: list[bytes] = field(default_factory=list)
    fetched_messages: dict[str, tuple[bytes, bytes]] = field(default_factory=dict)

    def list_mailboxes(self) -> tuple[str, list[bytes]]:
        self.calls.append(("LIST", ()))
        return "OK", self.mailbox_responses

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("LOGOUT", ()))
        return "BYE", []

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("SELECT", (mailbox, readonly)))
        return "OK", [b"1"]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.calls.append((command, args))
        if command == "SEARCH":
            return "OK", [self.search_results.pop(0)]
        if command == "STORE":
            return "OK", [b"20"]
        if command == "FETCH":
            uid = str(args[0])
            metadata, message = self.fetched_messages[uid]
            return "OK", [(metadata, message), b")"]
        raise AssertionError(f"Unexpected IMAP command: {command}")

    def expunge(self) -> tuple[str, list[bytes]]:
        self.calls.append(("EXPUNGE", ()))
        return "OK", [b"1"]


@dataclass
class FakeSmtpConnection:
    """Small programmable SMTP transport that records accepted messages."""

    messages: list[tuple[EmailMessage, str, list[str]]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def send_message(
        self,
        message: EmailMessage,
        *,
        sender: str,
        recipients: list[str],
    ) -> None:
        self.calls.append("SEND")
        self.messages.append((message, sender, recipients))

    def quit(self) -> None:
        self.calls.append("QUIT")


def _client(
    connection: FakeImapConnection,
    smtp_connection: FakeSmtpConnection | None = None,
) -> ProtonBridgeImapClient:
    configuration = ProtonBridgeConfiguration(
        username="hi@example.com",
        password_command="unused-in-test",  # noqa: S106  # pragma: allowlist secret
    )
    return ProtonBridgeImapClient(
        configuration,
        _password_reader,
        connection_factory=lambda _configuration, _password: connection,
        smtp_connection_factory=lambda _configuration, _password: (
            smtp_connection or FakeSmtpConnection()
        ),
    )


class TestProtonBridgeImapClient:
    def test_rejects_a_non_callable_password_reader(self):
        configuration = ProtonBridgeConfiguration(
            username="hi@example.com",
            password_command="unused-in-test",  # noqa: S106  # pragma: allowlist secret
        )

        with (
            pytest.warns(UserWarning, match="violates type hint.*Callable"),
            pytest.raises(ProtonMailError, match="password reader must be callable"),
        ):
            ProtonBridgeImapClient(configuration, object())

    def test_lists_international_mailboxes_with_a_display_name_and_raw_identifier(self):
        connection = FakeImapConnection(
            mailbox_responses=[b'(\\HasNoChildren) "/" "&Jjo-"'],
        )

        result = _client(connection).list_mailboxes()

        assert result.mailboxes[0].name == "☺"
        assert result.mailboxes[0].mailbox == "&Jjo-"

    def test_uses_the_returned_raw_mailbox_identifier_for_select(self):
        connection = FakeImapConnection(search_results=[b""])

        _client(connection).list_mail(mailbox="&Jjo-", limit=2, offset=0, unread=False)

        assert ("SELECT", ('"&Jjo-"', True)) in connection.calls

    def test_lists_unread_messages_with_peek_and_readonly_mailbox(self):
        connection = FakeImapConnection(
            search_results=[b"10"],
            fetched_messages={
                "10": (
                    b"10 (UID 10 FLAGS () BODY[HEADER] {82}",
                    (
                        b"Message-ID: <unseen@example.com>\r\n"
                        b"From: Unseen <unseen@example.com>\r\n\r\n"
                    ),
                ),
            },
        )

        result = _client(connection).list_mail(mailbox="INBOX", limit=2, offset=0, unread=True)

        assert [message.message_id for message in result.messages] == ["<unseen@example.com>"]
        assert ("SELECT", ('"INBOX"', True)) in connection.calls
        assert ("SEARCH", (None, "UNSEEN")) in connection.calls
        fetch_calls = [args for command, args in connection.calls if command == "FETCH"]
        assert all("BODY.PEEK[HEADER]" in str(args) for args in fetch_calls)
        assert not any(command == "STORE" for command, _args in connection.calls)

    def test_reads_by_message_id_without_mutating_seen_state(self):
        connection = FakeImapConnection(
            search_results=[b"20"],
            fetched_messages={
                "20": (
                    b"20 (UID 20 FLAGS () BODY[] {100}",
                    (
                        b"Message-ID: <event@example.com>\r\nSubject: Event\r\n"
                        b"X-Trace: one\r\n\r\nHello world"
                    ),
                )
            },
        )

        result = _client(connection).read_mail(
            mailbox="INBOX",
            message_id="<event@example.com>",
            include_headers=True,
        )

        assert result.body == "Hello world"
        assert [(header.name, header.value) for header in result.headers or []] == [
            ("Message-ID", "<event@example.com>"),
            ("Subject", "Event"),
            ("X-Trace", "one"),
        ]
        assert (
            "SEARCH",
            (None, "HEADER", "Message-ID", '"<event@example.com>"'),
        ) in connection.calls
        assert ("FETCH", ("20", "(UID FLAGS BODY.PEEK[])")) in connection.calls
        assert not any(command == "STORE" for command, _args in connection.calls)

    def test_rejects_ambiguous_message_id(self):
        connection = FakeImapConnection(search_results=[b"20 21"])

        with pytest.raises(ProtonMailError, match="ambiguous"):
            _client(connection).read_mail(
                mailbox="INBOX",
                message_id="<event@example.com>",
                include_headers=False,
            )

    def test_rejects_newlines_in_message_id_before_the_search_command(self):
        connection = FakeImapConnection()

        with pytest.raises(ProtonMailError, match="single line"):
            _client(connection).read_mail(
                mailbox="INBOX",
                message_id="<event@example.com>\r\nALL",
                include_headers=False,
            )

    def test_sends_plain_text_mail_through_the_bridge_smtp_identity(self):
        connection = FakeImapConnection()
        smtp = FakeSmtpConnection()

        delivery = _client(connection, smtp).send_mail(
            recipients=["recipient@example.com"],
            subject="Canary",
            body="Safe test body",
        )

        message, sender, recipients = smtp.messages[0]
        assert delivery.message_id == message["Message-ID"]
        assert delivery.message_id.endswith("@pynchy.local>")
        assert message["From"] == "hi@example.com"
        assert message["To"] == "recipient@example.com"
        assert message["Subject"] == "Canary"
        assert message.get_content() == "Safe test body\n"
        assert sender == "hi@example.com"
        assert recipients == ["recipient@example.com"]
        assert smtp.calls == ["SEND", "QUIT"]

    def test_permanently_deletes_a_message_and_checks_its_absence(self):
        connection = FakeImapConnection(search_results=[b"20", b""])
        client = _client(connection)

        client.delete_mail(mailbox="INBOX", message_id="<canary@example.test>")

        assert ("SELECT", ('"INBOX"', False)) in connection.calls
        assert ("STORE", ("20", "+FLAGS.SILENT", "(\\Deleted)")) in connection.calls
        assert ("EXPUNGE", ()) in connection.calls
        assert client.message_exists(mailbox="INBOX", message_id="<canary@example.test>") is False


class TestCommandPasswordProvider:
    def test_uses_argv_not_a_shell_and_strips_its_output(self):
        completed_process = Mock(returncode=0, stdout="bridge-password\n")

        with patch(
            "pynchy.plugins.integrations.proton_bridge.subprocess.run",
            return_value=completed_process,
        ) as run:
            password = CommandPasswordProvider("security find-generic-password -w").get_password()

        assert password.get_secret_value() == "bridge-password"
        run.assert_called_once_with(
            ["security", "find-generic-password", "-w"],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )

    def test_rejects_a_failed_password_command_without_exposing_stderr(self):
        completed_process = Mock(returncode=1, stdout="", stderr="sensitive failure")

        with (
            patch(
                "pynchy.plugins.integrations.proton_bridge.subprocess.run",
                return_value=completed_process,
            ),
            pytest.raises(ProtonMailError, match="Could not retrieve"),
        ):
            CommandPasswordProvider("security find-generic-password -w").get_password()
