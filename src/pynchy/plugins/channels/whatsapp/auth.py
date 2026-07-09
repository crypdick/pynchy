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

from pynchy.config import get_settings


@dataclass
class _AuthState:
    done: asyncio.Event
    exit_code: int = 0


def _configure_neonize_event_loop() -> None:
    # Neonize keeps module-level loop references; patch both modules so events
    # and internal tasks bind to this running loop.
    loop = asyncio.get_running_loop()
    neonize_events.event_global_loop = loop
    neonize_client.event_global_loop = loop


def _auth_db_path() -> str:
    data_dir = get_settings().data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "neonize.db")


def _print_already_authenticated() -> None:
    print("[OK] Already authenticated with WhatsApp")  # allow: print-statements
    print("     Delete data/neonize.db to force re-authentication.")  # allow: print-statements


def _print_auth_instructions() -> None:
    print("Starting WhatsApp authentication...")  # allow: print-statements
    print("Scan the QR code with WhatsApp:")  # allow: print-statements
    print("  1. Open WhatsApp on your phone")  # allow: print-statements
    print("  2. Tap Settings -> Linked Devices -> Link a Device")  # allow: print-statements
    print("  3. Point your camera at the QR code below")  # allow: print-statements
    print()  # allow: print-statements


def _print_qr(qr_data: bytes) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_data)
    qr.make()
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    print(buf.getvalue(), flush=True)  # allow: print-statements


def _register_auth_callbacks(client: NewAClient, state: _AuthState, auth_db: str) -> None:
    @client.event.qr  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_qr(_client: NewAClient, qr_data: bytes) -> None:
        _print_qr(qr_data)

    @client.event(ConnectedEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_connected(_client: NewAClient, _ev: ConnectedEv) -> None:
        print()  # allow: print-statements
        print("[OK] Successfully authenticated with WhatsApp")  # allow: print-statements
        print(f"     Credentials saved to {auth_db}")  # allow: print-statements
        print("     You can now run pynchy.")  # allow: print-statements
        state.done.set()

    @client.event(PairStatusEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_pair_status(_client: NewAClient, ev: PairStatusEv) -> None:
        print(f"  Paired as {ev.ID.User}")  # allow: print-statements

    @client.event(LoggedOutEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_logged_out(_client: NewAClient, _ev: LoggedOutEv) -> None:
        print()  # allow: print-statements
        print(
            "[ERROR] Logged out. Delete data/neonize.db and try again."
        )  # allow: print-statements
        state.exit_code = 1
        state.done.set()

    @client.event(ConnectFailureEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
    async def on_connect_failure(_client: NewAClient, _ev: ConnectFailureEv) -> None:
        print()  # allow: print-statements
        print("[ERROR] Connection failed. Please try again.")  # allow: print-statements
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


async def authenticate() -> None:
    """Authenticate WhatsApp by scanning a QR code.

    This is an interactive CLI entry point (``pynchy-whatsapp-auth``): it renders
    a QR code and step-by-step instructions to the terminal for the user to scan.
    The ``print()`` calls below are intentional user-facing console output — they
    must reach stdout regardless of log configuration, so they are exempted from
    the structured-logging rule.
    """
    _configure_neonize_event_loop()
    auth_db = _auth_db_path()
    client = NewAClient(auth_db)

    if await client.is_logged_in:
        _print_already_authenticated()
        return

    _print_auth_instructions()
    state = _AuthState(done=asyncio.Event())
    _register_auth_callbacks(client, state, auth_db)
    sys.exit(await _wait_for_auth_completion(client, state))


def main() -> None:
    try:
        asyncio.run(authenticate())
    except KeyboardInterrupt:
        print()  # allow: print-statements
        print("Authentication cancelled.")  # allow: print-statements
        sys.exit(1)


if __name__ == "__main__":
    main()
