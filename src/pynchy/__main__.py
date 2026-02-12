"""Entry point for `python -m pynchy`."""

from __future__ import annotations

import asyncio

from pynchy.app import PynchyApp


def main() -> None:
    app = PynchyApp()
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
