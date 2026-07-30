"""Tests for direct Proton Bridge IMAP transport semantics."""

from __future__ import annotations

import imaplib
import smtplib
import subprocess  # noqa: S404 - tests patch the administrator-configured command runner.
from dataclasses import dataclass, field
from email.message import EmailMessage
from unittest.mock import Mock, patch

import pytest
from pydantic import SecretStr

from pynchy.plugins.integrations.proton_bridge import (
    CommandPasswordProvider,
    ProtonBridgeConfiguration,
    ProtonBridgeImapClient,
    ProtonMailError,
    create_proton_mail_client,
)
from pynchy.plugins.integrations.proton_bridge_smtp import (
    BridgeSmtpConnection,
    ProtonBridgeSmtpError,
    open_bridge_smtp_connection,
)

_TEST_PASSWORD_COMMAND = "read-bridge-password"  # noqa: S105  # pragma: allowlist secret


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


class SearchWithoutDataConnection(FakeImapConnection):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "SEARCH":
            self.calls.append((command, args))
            return "OK", []
        return super().uid(command, *args)


class UnexpectedFetchConnection(FakeImapConnection):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "FETCH":
            self.calls.append((command, args))
            return "OK", [(b"metadata", "not bytes"), b"unexpected"]
        return super().uid(command, *args)


class FailedListConnection(FakeImapConnection):
    def list_mailboxes(self) -> tuple[str, list[bytes]]:
        self.calls.append(("LIST", ()))
        return "NO", []


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
    def test_normalizes_a_bridge_username_and_rejects_missing_environment(self):
        configuration = ProtonBridgeConfiguration(
            username=" mail@example.test ",
            password_command=_TEST_PASSWORD_COMMAND,
        )

        assert configuration.username == "mail@example.test"
        with pytest.raises(ProtonMailError, match="PYNCHY_PROTON_BRIDGE_USERNAME"):
            ProtonBridgeConfiguration.from_environment({})
        with pytest.raises(ValueError, match="single non-empty line"):
            ProtonBridgeConfiguration(
                username="mail\n@example.test",
                password_command=_TEST_PASSWORD_COMMAND,
            )

    def test_reads_bridge_settings_from_an_explicit_mcp_environment(self):
        configuration = ProtonBridgeConfiguration.from_environment(
            {
                "PYNCHY_PROTON_BRIDGE_USERNAME": "mail@example.test",
                "PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND": _TEST_PASSWORD_COMMAND,
                "PYNCHY_PROTON_BRIDGE_IMAP_PORT": "2143",
                "PYNCHY_PROTON_BRIDGE_SMTP_PORT": "2025",
            }
        )

        assert configuration.username == "mail@example.test"
        assert configuration.imap_port == 2143
        assert configuration.smtp_port == 2025

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

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            (b"", "invalid mailbox response"),
            (b'(\\HasNoChildren) "/" NIL', "without a name"),
            (b'(\\HasNoChildren) "/" \xff', "invalid mailbox identifier"),
            (b'(\\HasNoChildren) "/" "&bad', "invalid international mailbox name"),
            (b'(\\HasNoChildren) "/" "&!!!-"', "invalid international mailbox name"),
        ],
    )
    def test_rejects_malformed_mailbox_responses(self, response, message):
        with pytest.raises(ProtonMailError, match=message):
            _client(FakeImapConnection(mailbox_responses=[response])).list_mailboxes()

    def test_decodes_literal_ampersands_and_plain_mailbox_names(self):
        connection = FakeImapConnection(
            mailbox_responses=[
                b'(\\HasNoChildren) "/" "&-"',
                b'(\\HasNoChildren) "/" INBOX',
            ]
        )

        result = _client(connection).list_mailboxes()

        assert [(mailbox.name, mailbox.mailbox) for mailbox in result.mailboxes] == [
            ("&", "&-"),
            ("INBOX", "INBOX"),
        ]

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

    def test_rejects_a_message_without_a_message_id(self):
        connection = FakeImapConnection(
            search_results=[b"20"],
            fetched_messages={"20": (b"20 (UID 20 FLAGS ())", b"Subject: Missing ID\r\n\r\nbody")},
        )

        with pytest.raises(ProtonMailError, match="without a Message-ID"):
            _client(connection).read_mail(
                mailbox="INBOX",
                message_id="<missing@example.com>",
                include_headers=False,
            )

    def test_rejects_a_missing_message_id(self):
        connection = FakeImapConnection(search_results=[b""])

        with pytest.raises(ProtonMailError, match="was not found"):
            _client(connection).read_mail(
                mailbox="INBOX",
                message_id="<missing@example.com>",
                include_headers=False,
            )

    def test_handles_a_search_response_without_data(self):
        with pytest.raises(ProtonMailError, match="was not found"):
            _client(SearchWithoutDataConnection()).read_mail(
                mailbox="INBOX",
                message_id="<missing@example.com>",
                include_headers=False,
            )

    def test_returns_no_messages_for_a_list_search_without_data(self):
        result = _client(SearchWithoutDataConnection()).list_mail(
            mailbox="INBOX", limit=1, offset=0, unread=False
        )

        assert result.messages == []

    def test_rejects_an_unexpected_fetch_response(self):
        connection = UnexpectedFetchConnection(search_results=[b"20"])

        with pytest.raises(ProtonMailError, match="unexpected message response"):
            _client(connection).read_mail(
                mailbox="INBOX",
                message_id="<event@example.com>",
                include_headers=False,
            )

    def test_wraps_imap_connection_failures(self):
        configuration = ProtonBridgeConfiguration(
            username="hi@example.com",
            password_command="unused-in-test",  # noqa: S106  # pragma: allowlist secret
        )
        for failure in (OSError("socket closed"), imaplib.IMAP4.error("bad response")):
            client = ProtonBridgeImapClient(
                configuration,
                _password_reader,
                connection_factory=lambda _configuration, _password, failure=failure: (
                    _ for _ in ()
                ).throw(failure),
            )
            with pytest.raises(ProtonMailError, match="IMAP request failed"):
                client.list_mailboxes()

    def test_wraps_smtp_connection_failures(self):
        configuration = ProtonBridgeConfiguration(
            username="hi@example.com",
            password_command="unused-in-test",  # noqa: S106  # pragma: allowlist secret
        )
        client = ProtonBridgeImapClient(
            configuration,
            _password_reader,
            connection_factory=lambda _configuration, _password: FakeImapConnection(),
            smtp_connection_factory=lambda _configuration, _password: (_ for _ in ()).throw(
                ProtonBridgeSmtpError("SMTP unavailable")
            ),
        )

        with pytest.raises(ProtonMailError, match="SMTP request failed"):
            client.send_mail(recipients=["recipient@example.com"], subject="s", body="b")

    def test_preserves_password_reader_errors_during_smtp_setup(self):
        configuration = ProtonBridgeConfiguration(
            username="hi@example.com",
            password_command="unused-in-test",  # noqa: S106  # pragma: allowlist secret
        )

        def password_reader() -> SecretStr:
            raise ProtonMailError("credential unavailable")

        client = ProtonBridgeImapClient(
            configuration,
            password_reader,
            connection_factory=lambda _configuration, _password: FakeImapConnection(),
            smtp_connection_factory=lambda _configuration, _password: FakeSmtpConnection(),
        )

        with pytest.raises(ProtonMailError, match="credential unavailable"):
            client.send_mail(recipients=["recipient@example.com"], subject="s", body="b")

    def test_wraps_a_failed_list_status(self):
        with pytest.raises(ProtonMailError, match="could not list mailboxes"):
            _client(FailedListConnection()).list_mailboxes()

    def test_uses_the_default_tls_imap_adapter(self):
        configuration = ProtonBridgeConfiguration(
            username="mail@example.com",
            password_command="unused-in-test",  # noqa: S106  # pragma: allowlist secret
        )
        imap_client = Mock(spec=imaplib.IMAP4)
        imap_client.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', "ignored"])
        imap_client.logout.return_value = ("BYE", [b"logged out", "ignored"])
        imap_client.select.return_value = ("OK", [b"1"])
        imap_client.expunge.return_value = ("OK", [b"1"])
        tls_context = Mock()

        with (
            patch(
                "pynchy.plugins.integrations.proton_bridge.imaplib.IMAP4",
                return_value=imap_client,
            ) as imap,
            patch(
                "pynchy.plugins.integrations.proton_bridge.ssl.create_default_context",
                return_value=tls_context,
            ),
        ):
            result = ProtonBridgeImapClient(configuration, _password_reader).list_mailboxes()

            imap_client.uid.side_effect = [
                ("OK", [b"20"]),
                (
                    "OK",
                    [(b"20 (UID 20 FLAGS (\\Seen))", b"Message-ID: <event@example.com>\r\n\r\n")],
                ),
            ]
            listed = ProtonBridgeImapClient(configuration, _password_reader).list_mail(
                mailbox="INBOX", limit=1, offset=0, unread=False
            )

            imap_client.uid.side_effect = [
                ("OK", [b"20"]),
                ("OK", [b"20"]),
            ]
            ProtonBridgeImapClient(configuration, _password_reader).delete_mail(
                mailbox="INBOX", message_id="<event@example.com>"
            )

        assert result.mailboxes[0].mailbox == "INBOX"
        assert imap.call_count == 3
        assert imap.call_args_list == [
            (("127.0.0.1", 1143), {"timeout": 30}),
            (("127.0.0.1", 1143), {"timeout": 30}),
            (("127.0.0.1", 1143), {"timeout": 30}),
        ]
        assert imap_client.starttls.call_count == 3
        assert imap_client.login.call_count == 3
        imap_client.login.assert_called_with("mail@example.com", "test-bridge-password")
        assert tls_context.check_hostname is False
        assert tls_context.verify_mode == 0
        assert listed.messages[0].seen is True
        assert imap_client.logout.call_count == 3

    def test_creates_a_client_from_the_mcp_environment(self):
        client = create_proton_mail_client(
            environment={
                "PYNCHY_PROTON_BRIDGE_USERNAME": "mail@example.com",
                (
                    "PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND"
                ): "read-password",  # pragma: allowlist secret
            }
        )

        assert isinstance(client, ProtonBridgeImapClient)

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


class TestProtonBridgeSmtpTransport:
    def test_sends_messages_and_wraps_refused_recipients(self):
        client = Mock(spec=smtplib.SMTP)
        message = EmailMessage()
        transport = BridgeSmtpConnection(client)
        client.send_message.return_value = None

        transport.send_message(
            message, sender="sender@example.com", recipients=["recipient@example.com"]
        )
        client.send_message.assert_called_once_with(
            message,
            from_addr="sender@example.com",
            to_addrs=["recipient@example.com"],
        )

        client.send_message.return_value = {"recipient@example.com": (550, b"denied")}
        with pytest.raises(ProtonBridgeSmtpError, match="rejected a recipient"):
            transport.send_message(
                message, sender="sender@example.com", recipients=["recipient@example.com"]
            )

    def test_wraps_smtp_send_and_shutdown_failures(self):
        client = Mock(spec=smtplib.SMTP)
        transport = BridgeSmtpConnection(client)
        client.send_message.side_effect = smtplib.SMTPException("bridge unavailable")

        with pytest.raises(ProtonBridgeSmtpError, match="request failed"):
            transport.send_message(EmailMessage(), sender="sender@example.com", recipients=[])

        client.quit.side_effect = OSError("socket closed")
        with pytest.raises(ProtonBridgeSmtpError, match="shutdown failed"):
            transport.quit()

    def test_opens_loopback_smtp_with_tls_before_authentication(self):
        configuration = ProtonBridgeConfiguration(
            username="mail@example.com",
            password_command="unused-in-test",  # noqa: S106  # pragma: allowlist secret
            smtp_port=2025,
        )
        client = Mock(spec=smtplib.SMTP)

        with patch(
            "pynchy.plugins.integrations.proton_bridge_smtp.smtplib.SMTP",
            return_value=client,
        ) as smtp:
            connection = open_bridge_smtp_connection(
                configuration, SecretStr("test-bridge-password")
            )

        assert isinstance(connection, BridgeSmtpConnection)
        smtp.assert_called_once_with("127.0.0.1", 2025, timeout=30)
        assert client.ehlo.call_count == 2
        context = client.starttls.call_args.kwargs["context"]
        assert context.check_hostname is False
        assert context.verify_mode == 0
        client.login.assert_called_once_with("mail@example.com", "test-bridge-password")

    def test_open_wraps_connection_and_authentication_failures(self):
        configuration = ProtonBridgeConfiguration(
            username="mail@example.com",
            password_command="unused-in-test",  # noqa: S106  # pragma: allowlist secret
            smtp_port=2025,
        )
        with (
            patch(
                "pynchy.plugins.integrations.proton_bridge_smtp.smtplib.SMTP",
                side_effect=OSError("bridge unavailable"),
            ),
            pytest.raises(ProtonBridgeSmtpError, match="request failed"),
        ):
            open_bridge_smtp_connection(configuration, SecretStr("test-bridge-password"))

        client = Mock(spec=smtplib.SMTP)
        client.login.side_effect = smtplib.SMTPException("invalid credentials")
        with (
            patch(
                "pynchy.plugins.integrations.proton_bridge_smtp.smtplib.SMTP",
                return_value=client,
            ),
            pytest.raises(ProtonBridgeSmtpError, match="request failed"),
        ):
            open_bridge_smtp_connection(configuration, SecretStr("test-bridge-password"))


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

    @pytest.mark.parametrize(
        ("command", "message"),
        [
            ("'unterminated", "not valid"),
            ("   ", "is empty"),
        ],
    )
    def test_rejects_invalid_or_empty_password_commands(self, command, message):
        with pytest.raises(ProtonMailError, match=message):
            CommandPasswordProvider(command).get_password()

    @pytest.mark.parametrize(
        "failure",
        [OSError("command unavailable"), subprocess.TimeoutExpired("command", 15)],
    )
    def test_wraps_password_command_process_failures(self, failure):
        with (
            patch(
                "pynchy.plugins.integrations.proton_bridge.subprocess.run",
                side_effect=failure,
            ),
            pytest.raises(ProtonMailError, match="Could not retrieve"),
        ):
            CommandPasswordProvider("security find-generic-password -w").get_password()

    def test_rejects_password_command_output_without_a_password(self):
        with (
            patch(
                "pynchy.plugins.integrations.proton_bridge.subprocess.run",
                return_value=Mock(returncode=0, stdout="\n"),
            ),
            pytest.raises(ProtonMailError, match="returned no password"),
        ):
            CommandPasswordProvider("security find-generic-password -w").get_password()
