"""WhatsApp authentication script.

Run this during setup to authenticate with WhatsApp.
Displays QR code, waits for scan, saves credentials to SQLite, then exits.

Usage: uv run python -m pynchy.auth.whatsapp
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

from neonize.aioze.client import NewAClient
from neonize.events import ConnectedEv, ConnectFailureEv, LoggedOutEv, PairStatusEv

# Resolve store dir relative to project root (same as config.py)
STORE_DIR = (Path.cwd() / "store").resolve()


async def authenticate() -> None:
    auth_db = str(STORE_DIR / "neonize.db")
    STORE_DIR.mkdir(parents=True, exist_ok=True)

    client = NewAClient(auth_db)

    # Check if already authenticated
    if await client.is_logged_in:
        print("\u2713 Already authenticated with WhatsApp")
        print("  To re-authenticate, delete the store/neonize.db file and run again.")
        return

    print("Starting WhatsApp authentication...\n")
    print("Scan the QR code with WhatsApp:")
    print("  1. Open WhatsApp on your phone")
    print("  2. Tap Settings \u2192 Linked Devices \u2192 Link a Device")
    print("  3. Point your camera at the QR code below\n")

    # neonize displays the QR code automatically via segno
    # We just need to handle connection events

    done = asyncio.Event()
    exit_code = 0

    @client.event(ConnectedEv)
    async def on_connected(_client: NewAClient, _ev: ConnectedEv) -> None:
        print("\n\u2713 Successfully authenticated with WhatsApp!")
        print(f"  Credentials saved to {auth_db}")
        print("  You can now start the pynchy service.\n")
        done.set()

    @client.event(PairStatusEv)
    async def on_pair_status(_client: NewAClient, ev: PairStatusEv) -> None:
        print(f"  Paired as {ev.ID.User}")

    @client.event(LoggedOutEv)
    async def on_logged_out(_client: NewAClient, _ev: LoggedOutEv) -> None:
        nonlocal exit_code
        print("\n\u2717 Logged out. Delete store/neonize.db and try again.")
        exit_code = 1
        done.set()

    @client.event(ConnectFailureEv)
    async def on_connect_failure(_client: NewAClient, _ev: ConnectFailureEv) -> None:
        nonlocal exit_code
        print("\n\u2717 Connection failed. Please try again.")
        exit_code = 1
        done.set()

    await client.connect()

    # Run idle in background so events keep firing
    idle_task = asyncio.ensure_future(client.idle())

    # Wait for auth to complete or fail
    await done.wait()

    idle_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await idle_task

    sys.exit(exit_code)


def main() -> None:
    try:
        asyncio.run(authenticate())
    except KeyboardInterrupt:
        print("\nAuthentication cancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
