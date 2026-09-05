"""WhatsApp authentication helper.

Run this once to link a WhatsApp account and persist Neonize credentials.

Usage:
    uv run pynchy-whatsapp-auth
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from dataclasses import dataclass

import qrcode
from neonize.aioze import client as neonize_client
from neonize.aioze import events as neonize_events
from neonize.aioze.client import NewAClient
from neonize.events import ConnectedEv, ConnectFailureEv, LoggedOutEv, PairStatusEv


@dataclass
class _AuthState:
    done: asyncio.Event
    exit_code: int = 0


def _stdout_line(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _stdout_text(text: str) -> None:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _configure_neonize_event_loop() -> None:
    # Neonize keeps module-level loop references; patch both modules so events
    # and internal tasks bind to this running loop.
    loop = asyncio.get_running_loop()
    neonize_events.event_global_loop = loop  # noqa: V101
    neonize_client.event_global_loop = loop  # noqa: V101


def _print_already_authenticated() -> None:
    _stdout_line("[OK] Already authenticated with WhatsApp")
    _stdout_line("     Delete data/neonize.db to force re-authentication.")


def _print_auth_instructions() -> None:
    _stdout_line("Starting WhatsApp authentication...")
    _stdout_line("Scan the QR code with WhatsApp:")
    _stdout_line("  1. Open WhatsApp on your phone")
    _stdout_line("  2. Tap Settings -> Linked Devices -> Link a Device")
    _stdout_line("  3. Point your camera at the QR code below")
    _stdout_line()


def _print_qr(qr_data: bytes) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_data)
    qr.make()
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    _stdout_text(buf.getvalue())


def _register_auth_callbacks(client: NewAClient, state: _AuthState, auth_db: str) -> None:
    @client.event.qr  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_qr(_client: NewAClient, qr_data: bytes) -> None:  # noqa: RUF029 - neonize may await events.
        _print_qr(qr_data)

    @client.event(ConnectedEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_connected(_client: NewAClient, _ev: ConnectedEv) -> None:  # noqa: RUF029 - neonize may await events.
        _stdout_line()
        _stdout_line("[OK] Successfully authenticated with WhatsApp")
        _stdout_line(f"     Credentials saved to {auth_db}")
        _stdout_line("     You can now run pynchy.")
        state.done.set()

    @client.event(PairStatusEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_pair_status(_client: NewAClient, ev: PairStatusEv) -> None:  # noqa: RUF029 - neonize may await events.
        _stdout_line(f"  Paired as {ev.ID.User}")

    @client.event(LoggedOutEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_logged_out(_client: NewAClient, _ev: LoggedOutEv) -> None:  # noqa: RUF029 - neonize may await events.
        _stdout_line()
        _stdout_line("[ERROR] Logged out. Delete data/neonize.db and try again.")
        state.exit_code = 1
        state.done.set()

    @client.event(ConnectFailureEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_connect_failure(_client: NewAClient, _ev: ConnectFailureEv) -> None:  # noqa: RUF029 - neonize may await events.
        _stdout_line()
        _stdout_line("[ERROR] Connection failed. Please try again.")
        state.exit_code = 1
        state.done.set()


async def _wait_for_auth_completion(client: NewAClient, state: _AuthState) -> int:
    await client.connect()
    idle_task = asyncio.ensure_future(client.idle())
    await state.done.wait()

    idle_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await idle_task

    return state.exit_code


async def authenticate(auth_db: str) -> None:
    """Authenticate WhatsApp by scanning a QR code.

    This is an interactive CLI entry point (``pynchy-whatsapp-auth``): it renders
    a QR code and step-by-step instructions to the terminal for the user to scan.
    The stdout writes below are intentional user-facing console output — they
    must reach stdout regardless of log configuration.
    """
    _configure_neonize_event_loop()
    client = NewAClient(auth_db)

    if await client.is_logged_in:
        _print_already_authenticated()
        return

    _print_auth_instructions()
    state = _AuthState(done=asyncio.Event())
    _register_auth_callbacks(client, state, auth_db)
    sys.exit(await _wait_for_auth_completion(client, state))


def main(auth_db: str) -> None:
    try:
        asyncio.run(authenticate(auth_db))
    except KeyboardInterrupt:
        _stdout_line()
        _stdout_line("Authentication cancelled.")
        sys.exit(1)


if __name__ == "__main__":
    raise SystemExit("Run `uv run pynchy-whatsapp-auth` instead.")
