"""Entry point for `python -m pynchy` / `uv run pynchy`.

Subcommands:
    pynchy              Run the service (default)
    pynchy --tui        Attach TUI client to a running instance
    pynchy build        Build the container image
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess  # noqa: S404, RUF100 - fixed no-shell container runtime argv below.
import sys
from pathlib import Path

_DEFAULT_PORT = "8484"
_DEFAULT_HOST = f"localhost:{_DEFAULT_PORT}"


def _stdout_line(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _stderr_line(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def _run() -> None:
    from dotenv import load_dotenv

    load_dotenv()  # Make .env vars available in os.environ for env_forward, etc.

    from pynchy.host.orchestrator.app import PynchyApp

    app = PynchyApp()
    asyncio.run(app.run())


def _tui(host: str) -> None:
    from pynchy.plugins.channels.tui.client import run_tui

    run_tui(host)


def _build() -> None:
    from pynchy.config import get_settings
    from pynchy.plugins.runtimes.detection import get_runtime

    s = get_settings()
    runtime = get_runtime()
    container_dir = s.project_root / "src" / "pynchy" / "agent"

    if not (container_dir / "Dockerfile").exists():
        _stderr_line(f"Error: No Dockerfile at {container_dir / 'Dockerfile'}")
        sys.exit(1)

    _stdout_line(f"Building {s.container.image} with {runtime.cli}...")
    result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is selected by trusted runtime detection and argv is fixed.
        [runtime.cli, "build", "-t", s.container.image, "."],
        cwd=str(container_dir),
        check=False,
    )
    sys.exit(result.returncode)


def _prune_migration_backups(path: str | None, keep: int, *, apply: bool) -> None:
    from pynchy.config import get_settings
    from pynchy.host.migration_backups import prune_migration_backups

    backups_dir = Path(path) if path else get_settings().project_root / "data" / "migration-backups"
    result = prune_migration_backups(backups_dir, keep=keep, dry_run=not apply)
    action = "Removed" if apply else "Would remove"

    sys.stdout.write(f"Migration backups: {backups_dir}\n")
    sys.stdout.write(f"Keeping {len(result.kept)} backup(s).\n")
    if not result.removed:
        sys.stdout.write("No older backup directories to remove.\n")
        return

    for removed_path in result.removed:
        sys.stdout.write(f"{action}: {removed_path}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pynchy",
        description="Personal AI assistant",
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
    sub.add_parser("build", help="Build the container image")
    prune = sub.add_parser(
        "prune-migration-backups",
        help="Prune old data/migration-backups directories",
    )
    prune.add_argument(
        "path",
        nargs="?",
        help="Migration-backups directory (default: data/migration-backups under project root)",
    )
    prune.add_argument(
        "--keep",
        type=int,
        default=3,
        help="Number of newest backup directories to keep (default: 3)",
    )
    prune.add_argument(
        "--apply",
        action="store_true",
        help="Delete old backup directories. Without this flag, only report what would be removed.",
    )

    args = parser.parse_args()

    match args.command:
        case "build":
            _build()
        case "prune-migration-backups":
            _prune_migration_backups(args.path, keep=args.keep, apply=args.apply)
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
