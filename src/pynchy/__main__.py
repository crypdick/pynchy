"""Entry point for `python -m pynchy` / `uv run pynchy`.

Subcommands:
    pynchy              Run the service (default)
    pynchy --tui        Attach TUI client to a running instance
    pynchy build        Build the container image
    pynchy doctor       Explain effective workspace host-action capabilities
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess  # noqa: S404, RUF100 - fixed no-shell container runtime argv below.
import sys
import urllib.parse
import urllib.request
from collections.abc import Mapping
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
    from dotenv import (  # noqa: PLC0415, RUF100 - CLI entrypoint keeps heavy app imports lazy.
        load_dotenv,
    )

    load_dotenv()  # Make .env vars available in os.environ for env_forward, etc.

    from pynchy.host.orchestrator.app import (  # noqa: PLC0415, RUF100 - avoid importing the orchestrator for --help and other subcommands.
        PynchyApp,
    )

    app = PynchyApp()
    asyncio.run(app.run())


def _tui(host: str) -> None:
    from pynchy.plugins.channels.tui.client import (  # noqa: PLC0415, RUF100 - TUI dependencies are only needed for --tui.
        run_tui,
    )

    run_tui(host)


def _build() -> None:
    from pynchy.config import (  # noqa: PLC0415, RUF100 - build command loads settings only when invoked.
        get_settings,
    )
    from pynchy.host.container_manager.cleanup import (  # noqa: PLC0415, RUF100 - runtime cleanup is build-command specific.
        cleanup_runtime_builder,
    )
    from pynchy.plugins.runtimes.detection import (  # noqa: PLC0415, RUF100 - runtime probing is build-command specific.
        get_runtime,
    )

    s = get_settings()
    runtime = get_runtime()
    container_dir = s.project_root / "src" / "pynchy" / "agent"

    if not (container_dir / "Dockerfile").exists():
        _stderr_line(f"Error: No Dockerfile at {container_dir / 'Dockerfile'}")
        sys.exit(1)

    _stdout_line(f"Building {s.container.image} with {runtime.cli}...")
    try:
        result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is selected by trusted runtime detection and argv is fixed.
            [runtime.cli, "build", "-t", s.container.image, "."],
            cwd=str(container_dir),
            check=False,
        )
    finally:
        cleanup_runtime_builder(runtime)
    sys.exit(result.returncode)


def _prune_migration_backups(path: str | None, keep: int, *, apply: bool) -> None:
    from pynchy.config import (  # noqa: PLC0415, RUF100 - prune command loads settings only when invoked.
        get_settings,
    )
    from pynchy.host.migration_backups import (  # noqa: PLC0415, RUF100 - prune implementation is command specific.
        prune_migration_backups,
    )

    project_root = get_settings().project_root
    backups_dir = Path(path) if path else project_root / "data" / "migration-backups"
    result = prune_migration_backups(backups_dir, keep=keep, dry_run=not apply)
    action = "Removed" if apply else "Would remove"

    sys.stdout.write(f"Migration backups: {backups_dir}\n")
    sys.stdout.write(f"Keeping {len(result.kept)} backup(s).\n")
    if not result.removed:
        sys.stdout.write("No older backup directories to remove.\n")
        return

    for removed_path in result.removed:
        sys.stdout.write(f"{action}: {removed_path}\n")


def _doctor_url(host: str, workspace: str | None) -> str:
    base = host if "://" in host else f"http://{host}"
    url = f"{base.rstrip('/')}/capabilities"
    return f"{url}?{urllib.parse.urlencode({'workspace': workspace})}" if workspace else url


def _render_capability_doctor(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise TypeError("Capability endpoint returned a non-object response")
    raw_workspaces = (
        [payload] if isinstance(payload.get("workspace"), str) else payload.get("workspaces", [])
    )
    if not isinstance(raw_workspaces, list):
        raise TypeError("Capability endpoint returned an invalid workspace list")
    if not raw_workspaces:
        return "No configured workspace capability snapshots."

    lines: list[str] = []
    for raw_workspace in raw_workspaces:
        if not isinstance(raw_workspace, Mapping):
            raise TypeError("Capability endpoint returned an invalid workspace snapshot")
        workspace = raw_workspace.get("workspace", "unknown")
        lines.append(f"Capabilities for {workspace}:")
        raw_capabilities = raw_workspace.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            raise TypeError("Capability endpoint returned an invalid capability list")
        for raw_capability in raw_capabilities:
            if not isinstance(raw_capability, Mapping):
                raise TypeError("Capability endpoint returned an invalid capability")
            capability_id = raw_capability.get("id", "unknown")
            status = raw_capability.get("status", "unknown")
            reason = raw_capability.get("reason")
            lines.append(
                f"  [{status}] {capability_id}"
                + (f" - {reason}" if isinstance(reason, str) and reason else "")
            )
            for field, label in (("setup_hint", "setup"), ("recovery_hint", "recover")):
                hint = raw_capability.get(field)
                if isinstance(hint, str) and hint:
                    lines.append(f"    {label}: {hint}")
    return "\n".join(lines)


def _doctor(host: str, workspace: str | None, *, json_output: bool) -> int:
    url = _doctor_url(host, workspace)
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310, RUF100 - operator-selected Pynchy status endpoint is intentionally queried.
            payload = json.loads(response.read())
        output = (
            json.dumps(payload, indent=2, sort_keys=True)
            if json_output
            else _render_capability_doctor(payload)
        )
    except (OSError, TypeError, ValueError) as exc:
        _stderr_line(f"Capability doctor failed: {exc}")
        return 1
    _stdout_line(output)
    return 0


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
    doctor = sub.add_parser(
        "doctor",
        help="Explain effective workspace host-action capabilities",
    )
    doctor.add_argument(
        "--workspace",
        help="Show one workspace instead of every configured workspace",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Print the raw capability snapshot as JSON",
    )

    args = parser.parse_args()

    match args.command:
        case "build":
            _build()
        case "prune-migration-backups":
            _prune_migration_backups(args.path, keep=args.keep, apply=args.apply)
        case "doctor":
            sys.exit(_doctor(args.host, args.workspace, json_output=args.json))
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
