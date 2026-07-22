"""Entry point for `python -m pynchy` / `uv run pynchy`.

Subcommands:
    pynchy              Run the service (default)
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
_DEFAULT_CONTROL_SOCKET = Path("data/pynchy.sock")
_DEFAULT_CONTROL_TOKEN_FILE = Path("data/control-plane.token")
_DEFAULT_CONTROL_TOKEN_ENV = "PYNCHY_CONTROL_TOKEN"  # noqa: S105, RUF100 - environment variable name, not a credential value.


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

    from pynchy.logger import (  # noqa: PLC0415, RUF100 - configure the error log before application startup.
        configure_error_log,
    )

    configure_error_log(Path("logs/pynchy.error.log"))

    from pynchy.host.orchestrator.app import (  # noqa: PLC0415, RUF100 - avoid importing the orchestrator for --help and other subcommands.
        PynchyApp,
    )

    app = PynchyApp()
    asyncio.run(app.run())


def _control_client_target(
    host: str | None,
    socket_path: Path | None,
) -> tuple[str | None, Path | None]:
    if host is not None:
        return host, None
    candidate = socket_path or _DEFAULT_CONTROL_SOCKET
    if socket_path is not None or candidate.exists():
        return None, candidate
    return _DEFAULT_HOST, None


def _control_client_token(token_file: Path | None) -> str | None:
    from pynchy.host.orchestrator.http_control import (  # noqa: PLC0415, RUF100 - client auth is only needed for control-plane commands.
        load_control_plane_client_token,
    )

    return load_control_plane_client_token(
        token_env=_DEFAULT_CONTROL_TOKEN_ENV,
        token_file=token_file or _DEFAULT_CONTROL_TOKEN_FILE,
    )


def _build() -> None:
    from pynchy.config import (  # noqa: PLC0415, RUF100 - build command loads settings only when invoked.
        get_settings,
    )
    from pynchy.host.container_manager.cleanup import (  # noqa: PLC0415, RUF100 - runtime cleanup is build-command specific.
        cleanup_runtime_build_state,
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
    runtime.ensure_running()
    if not cleanup_runtime_build_state(runtime):
        _stderr_line("Error: Could not clean stale container build state")
        sys.exit(1)
    build_state_cleaned = False
    try:
        result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is selected by trusted runtime detection and argv is fixed.
            [runtime.cli, "build", "-t", s.container.image, "."],
            cwd=str(container_dir),
            check=False,
        )
    finally:
        build_state_cleaned = cleanup_runtime_build_state(runtime)
    if result.returncode == 0 and not build_state_cleaned:
        _stderr_line("Error: Could not clean container build state after the build")
        sys.exit(1)
    sys.exit(result.returncode)


def _bootstrap_control_plane_token(*, rotate: bool) -> int:
    from pynchy.config import (  # noqa: PLC0415, RUF100 - bootstrap reads settings only for this subcommand.
        get_settings,
    )
    from pynchy.host.orchestrator.http_control import (  # noqa: PLC0415, RUF100 - token creation is isolated to the control-plane command.
        ControlPlaneConfigurationError,
        bootstrap_control_plane_token,
    )

    settings = get_settings()
    try:
        path = bootstrap_control_plane_token(
            settings.server,
            project_root=settings.project_root,
            rotate=rotate,
        )
    except (ControlPlaneConfigurationError, OSError) as exc:
        _stderr_line(f"Control-plane bootstrap failed: {exc}")
        return 1
    action = "Rotated" if rotate else "Created"
    _stdout_line(f"{action} permission-restricted control-plane token: {path}")
    return 0


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


async def _read_unix_json(
    socket_path: Path,
    relative_url: str,
    *,
    bearer_token: str | None,
) -> object:
    import aiohttp  # noqa: PLC0415, RUF100 - Unix HTTP client is only needed by local CLI control.

    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
    connector = aiohttp.UnixConnector(path=str(socket_path))
    async with (
        aiohttp.ClientSession(connector=connector, headers=headers) as session,
        session.get(f"http://localhost{relative_url}") as response,
    ):
        response.raise_for_status()
        return await response.json()


def _doctor(
    host: str | None,
    workspace: str | None,
    *,
    json_output: bool,
    socket_path: Path | None = None,
    token_file: Path | None = None,
) -> int:
    try:
        payload = _fetch_doctor_payload(
            host,
            workspace,
            socket_path=socket_path,
            token_file=token_file,
        )
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


def _fetch_doctor_payload(
    host: str | None,
    workspace: str | None,
    *,
    socket_path: Path | None,
    token_file: Path | None,
) -> object:
    selected_host, selected_socket = _control_client_target(host, socket_path)
    token = _control_client_token(token_file)
    if selected_socket is not None:
        relative_url = _doctor_url("localhost", workspace).removeprefix("http://localhost")
        return asyncio.run(_read_unix_json(selected_socket, relative_url, bearer_token=token))
    if selected_host is None:
        raise ValueError("No TCP host or Unix socket selected for the control-plane client")

    url = _doctor_url(selected_host, workspace)
    request: str | urllib.request.Request = url
    if token:
        request = urllib.request.Request(  # noqa: S310, RUF100 - operator-selected endpoint with bearer auth.
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310, RUF100 - operator-selected Pynchy status endpoint is intentionally queried.
        return json.loads(response.read())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pynchy",
        description="Personal AI assistant",
    )
    control_target = parser.add_mutually_exclusive_group()
    control_target.add_argument(
        "--host",
        help=f"Host:port of the Pynchy server (local default: Unix socket, then {_DEFAULT_HOST})",
    )
    control_target.add_argument(
        "--socket",
        type=Path,
        help=f"Unix control socket (default when present: {_DEFAULT_CONTROL_SOCKET})",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help=(
            "Bearer token file for remote control (default: PYNCHY_CONTROL_TOKEN, then "
            f"{_DEFAULT_CONTROL_TOKEN_FILE})"
        ),
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("build", help="Build the container image")
    control_plane = sub.add_parser(
        "control-plane",
        help="Bootstrap local credentials for authenticated control-plane access",
    )
    control_plane_subcommands = control_plane.add_subparsers(
        dest="control_plane_command",
        required=True,
    )
    bootstrap = control_plane_subcommands.add_parser(
        "bootstrap",
        help="Create a mode-0600 bearer token without printing it",
    )
    bootstrap.add_argument(
        "--rotate",
        action="store_true",
        help="Replace an existing token and invalidate clients that still use it",
    )
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
        case "control-plane":
            if args.control_plane_command == "bootstrap":
                sys.exit(_bootstrap_control_plane_token(rotate=args.rotate))
        case "prune-migration-backups":
            _prune_migration_backups(args.path, keep=args.keep, apply=args.apply)
        case "doctor":
            sys.exit(
                _doctor(
                    args.host,
                    args.workspace,
                    json_output=args.json,
                    socket_path=args.socket,
                    token_file=args.token_file,
                )
            )
        case _:
            _run()


if __name__ == "__main__":
    main()
