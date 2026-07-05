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

import qrcode
from neonize.aioze import client as neonize_client
from neonize.aioze import events as neonize_events
from neonize.aioze.client import NewAClient
from neonize.events import ConnectedEv, ConnectFailureEv, LoggedOutEv, PairStatusEv

from pynchy.config import get_settings


async def authenticate() -> None:
    """Authenticate WhatsApp by scanning a QR code.

    This is an interactive CLI entry point (``pynchy-whatsapp-auth``): it renders
    a QR code and step-by-step instructions to the terminal for the user to scan.
    The ``print()`` calls below are intentional user-facing console output — they
    must reach stdout regardless of log configuration, so they are exempted from
    the structured-logging rule.
    """
    # Neonize keeps module-level loop references; patch both modules so events
    # and internal tasks bind to this running loop.
    loop = asyncio.get_running_loop()
    neonize_events.event_global_loop = loop
    neonize_client.event_global_loop = loop

    data_dir = get_settings().data_dir
    auth_db = str(data_dir / "neonize.db")
    data_dir.mkdir(parents=True, exist_ok=True)

    client = NewAClient(auth_db)

    if await client.is_logged_in:
        print("[OK] Already authenticated with WhatsApp")  # allow: print-statements
        print("     Delete data/neonize.db to force re-authentication.")  # allow: print-statements
        return

    print("Starting WhatsApp authentication...")  # allow: print-statements
    print("Scan the QR code with WhatsApp:")  # allow: print-statements
    print("  1. Open WhatsApp on your phone")  # allow: print-statements
    print("  2. Tap Settings -> Linked Devices -> Link a Device")  # allow: print-statements
    print("  3. Point your camera at the QR code below")  # allow: print-statements
    print()  # allow: print-statements

    done = asyncio.Event()
    exit_code = 0

    @client.event.qr
    async def on_qr(_client: NewAClient, qr_data: bytes) -> None:
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_data)
        qr.make()
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        print(buf.getvalue(), flush=True)  # allow: print-statements

    @client.event(ConnectedEv)
    async def on_connected(_client: NewAClient, _ev: ConnectedEv) -> None:
        print()  # allow: print-statements
        print("[OK] Successfully authenticated with WhatsApp")  # allow: print-statements
        print(f"     Credentials saved to {auth_db}")  # allow: print-statements
        print("     You can now run pynchy.")  # allow: print-statements
        done.set()

    @client.event(PairStatusEv)
    async def on_pair_status(_client: NewAClient, ev: PairStatusEv) -> None:
        print(f"  Paired as {ev.ID.User}")  # allow: print-statements

    @client.event(LoggedOutEv)
    async def on_logged_out(_client: NewAClient, _ev: LoggedOutEv) -> None:
        nonlocal exit_code
        print()  # allow: print-statements
        print(
            "[ERROR] Logged out. Delete data/neonize.db and try again."
        )  # allow: print-statements
        exit_code = 1
        done.set()

    @client.event(ConnectFailureEv)
    async def on_connect_failure(_client: NewAClient, _ev: ConnectFailureEv) -> None:
        nonlocal exit_code
        print()  # allow: print-statements
        print("[ERROR] Connection failed. Please try again.")  # allow: print-statements
        exit_code = 1
        done.set()

    await client.connect()
    idle_task = asyncio.ensure_future(client.idle())
    await done.wait()

    idle_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await idle_task

    sys.exit(exit_code)


def main() -> None:
    try:
        asyncio.run(authenticate())
    except KeyboardInterrupt:
        print()  # allow: print-statements
        print("Authentication cancelled.")  # allow: print-statements
        sys.exit(1)


if __name__ == "__main__":
    main()
