"""Behavioral coverage for the WhatsApp authentication command."""

from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from conftest import make_settings

from pynchy import __main__ as cli

if TYPE_CHECKING:
    from pathlib import Path


def _install_module(name: str, *, package: bool = False) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []  # noqa: V101  # type: ignore[attr-defined]  # import package marker.
    sys.modules[name] = module
    return module


# WhatsApp is optional in the standard test environment.  The command is
# driven through its public behavior below, with only the provider boundary
# replaced by small SDK-shaped types.
neonize = _install_module("neonize", package=True)
aioze = _install_module("neonize.aioze", package=True)
aioze_client = _install_module("neonize.aioze.client")
aioze_events = _install_module("neonize.aioze.events")
neonize_events = _install_module("neonize.events")
neonize_utils = _install_module("neonize.utils", package=True)
neonize_jid = _install_module("neonize.utils.jid")
neonize_enum = _install_module("neonize.utils.enum")

neonize.aioze = aioze
aioze.client = aioze_client
aioze.events = aioze_events
neonize.utils = neonize_utils
neonize_utils.jid = neonize_jid
neonize_utils.enum = neonize_enum  # noqa: V101


class _NeonizeClient:
    pass


class _ConnectedEvent:
    pass


class _ConnectFailureEvent:
    pass


class _DisconnectedEvent:
    pass


class _LoggedOutEvent:
    pass


class _MessageEvent:
    pass


class _PairStatusEvent:
    pass


class _ChatPresence:
    CHAT_PRESENCE_COMPOSING = "composing"
    CHAT_PRESENCE_PAUSED = "paused"


class _ChatPresenceMedia:
    CHAT_PRESENCE_MEDIA_TEXT = "text"


aioze_client.NewAClient = _NeonizeClient
neonize_events.ConnectedEv = _ConnectedEvent
neonize_events.ConnectFailureEv = _ConnectFailureEvent
neonize_events.DisconnectedEv = _DisconnectedEvent
neonize_events.LoggedOutEv = _LoggedOutEvent
neonize_events.MessageEv = _MessageEvent
neonize_events.PairStatusEv = _PairStatusEvent
neonize_enum.ChatPresence = _ChatPresence
neonize_enum.ChatPresenceMedia = _ChatPresenceMedia
neonize_jid.Jid2String = lambda jid: getattr(jid, "value", "")
neonize_jid.build_jid = lambda *parts: parts


qrcode = _install_module("qrcode")


class _QRCode:
    def __init__(self, *, border: int) -> None:
        self.border = border

    def add_data(self, data: bytes) -> None:
        self.data = data

    def make(self) -> None:
        pass

    def print_ascii(self, *, out: Any, invert: bool) -> None:
        assert self.border == 1
        assert self.data == b"scan this code"
        assert invert is True
        out.write(self.output)

    output = "QR"


qrcode.QRCode = _QRCode

auth = importlib.import_module("pynchy.plugins.channels.whatsapp.auth")


class _EventRegistry:
    def __init__(self) -> None:
        self.handlers: dict[object, Any] = {}
        self.qr_handler: Any = None

    def __call__(self, event_type: object) -> Any:
        def register(handler: Any) -> Any:
            self.handlers[event_type] = handler
            return handler

        return register

    def qr(self, handler: Any) -> Any:
        self.qr_handler = handler
        return handler


@dataclass(frozen=True)
class _PairIdentity:
    User: str


@dataclass(frozen=True)
class _PairStatus:
    ID: _PairIdentity


class _AuthenticationClient(_NeonizeClient):
    """SDK-shaped client that completes the documented terminal outcomes."""

    logged_in = False
    outcome = "connected"

    def __init__(self, _auth_db: str) -> None:
        self.event = _EventRegistry()

    @property
    def is_logged_in(self) -> Any:
        return asyncio.sleep(0, result=self.logged_in)

    async def connect(self) -> None:
        if self.event.qr_handler is not None:
            await self.event.qr_handler(self, b"scan this code")
        await self.event.handlers[auth.PairStatusEv](
            self, _PairStatus(_PairIdentity("15551234567"))
        )
        event_type = {
            "connected": auth.ConnectedEv,
            "logged_out": auth.LoggedOutEv,
            "failed": auth.ConnectFailureEv,
        }[self.outcome]
        await self.event.handlers[event_type](self, object())

    async def idle(self) -> None:
        await asyncio.Event().wait()


async def test_authenticate_reports_existing_credentials_without_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _AuthenticationClient.logged_in = True
    _AuthenticationClient.outcome = "connected"
    monkeypatch.setattr(auth, "NewAClient", _AuthenticationClient)

    await auth.authenticate("data/neonize.db")

    assert capsys.readouterr().out == (
        "[OK] Already authenticated with WhatsApp\n"
        "     Delete data/neonize.db to force re-authentication.\n"
    )


@pytest.mark.parametrize(
    ("outcome", "exit_code", "expected_message"),
    [
        ("connected", 0, "[OK] Successfully authenticated with WhatsApp"),
        ("logged_out", 1, "[ERROR] Logged out. Delete data/neonize.db and try again."),
        ("failed", 1, "[ERROR] Connection failed. Please try again."),
    ],
)
async def test_authenticate_reports_the_provider_terminal_outcome(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    exit_code: int,
    expected_message: str,
) -> None:
    _AuthenticationClient.logged_in = False
    _AuthenticationClient.outcome = outcome
    monkeypatch.setattr(auth, "NewAClient", _AuthenticationClient)

    with pytest.raises(SystemExit) as exited:
        await auth.authenticate("data/neonize.db")

    assert exited.value.code == exit_code
    output = capsys.readouterr().out
    assert "Starting WhatsApp authentication..." in output
    assert "QR\n" in output
    assert "Paired as 15551234567" in output
    assert expected_message in output


async def test_authenticate_preserves_qr_output_that_already_ends_with_newline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _AuthenticationClient.logged_in = False
    _AuthenticationClient.outcome = "connected"
    monkeypatch.setattr(auth, "NewAClient", _AuthenticationClient)
    monkeypatch.setattr(_QRCode, "output", "QR\n")

    with pytest.raises(SystemExit) as exited:
        await auth.authenticate("data/neonize.db")

    assert exited.value.code == 0
    assert "QR\n" in capsys.readouterr().out


def test_main_turns_keyboard_interrupt_into_a_cancelled_authentication_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(coroutine: Any) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(auth.asyncio, "run", interrupted)

    with pytest.raises(SystemExit) as exited:
        auth.main("data/neonize.db")

    assert exited.value.code == 1
    assert capsys.readouterr().out == "\nAuthentication cancelled.\n"


def test_whatsapp_auth_cli_uses_the_configured_credential_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authenticate = Mock()
    monkeypatch.setattr(
        "pynchy.config.api.get_settings",
        lambda: make_settings(data_dir=tmp_path / "credentials"),
    )
    monkeypatch.setattr(auth, "main", authenticate)

    cli.whatsapp_auth()

    assert (tmp_path / "credentials").is_dir()
    authenticate.assert_called_once_with(str(tmp_path / "credentials" / "neonize.db"))
