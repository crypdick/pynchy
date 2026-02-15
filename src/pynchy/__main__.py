"""Entry point for `python -m pynchy` / `uv run pynchy`.

Subcommands:
    pynchy              Run the service (default)
    pynchy --tui        Attach TUI client to a running instance
    pynchy auth         Authenticate with WhatsApp
    pynchy build        Build the container image
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys

_DEFAULT_PORT = "8484"
_DEFAULT_HOST = f"localhost:{_DEFAULT_PORT}"


def _run() -> None:
    from pynchy.app import PynchyApp

    app = PynchyApp()
    asyncio.run(app.run())


def _tui(host: str) -> None:
    from pynchy.tui import run_tui

    run_tui(host)


def _auth() -> None:
    from pynchy.auth.whatsapp import main as auth_main

    auth_main()


def _build() -> None:
    from pynchy.config import get_settings
    from pynchy.runtime import get_runtime

    s = get_settings()
    runtime = get_runtime()
    container_dir = s.project_root / "container"

    if not (container_dir / "Dockerfile").exists():
        print(f"Error: No Dockerfile at {container_dir / 'Dockerfile'}", file=sys.stderr)
        sys.exit(1)

    print(f"Building {s.container.image} with {runtime.cli}...")
    result = subprocess.run(
        [runtime.cli, "build", "-t", s.container.image, "."],
        cwd=str(container_dir),
    )
    sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pynchy",
        description="Personal Claude assistant on WhatsApp",
    )
    parser.add_argument(
        "--tui", action="store_true", help="Attach TUI client to a running pynchy instance"
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"Host:port of the pynchy server (default: {_DEFAULT_HOST})",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("auth", help="Authenticate with WhatsApp")
    sub.add_parser("build", help="Build the container image")

    args = parser.parse_args()

    match args.command:
        case "auth":
            _auth()
        case "build":
            _build()
        case _:
            if args.tui:
                host = args.host
                if ":" not in host.split("//")[-1]:
                    host = f"{host}:{_DEFAULT_PORT}"
                _tui(host=host)
            else:
                _run()


if __name__ == "__main__":
    main()
