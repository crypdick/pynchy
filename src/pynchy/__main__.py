"""Entry point for `python -m pynchy` / `uv run pynchy`.

Subcommands:
    pynchy              Run the service (default)
    pynchy build        Build the container image
    pynchy publish-personalization  Publish the canonical personalization checkout
    pynchy status       Read service status through the authenticated control plane
    pynchy deploy       Request deployment through the authenticated control plane
    pynchy doctor       Explain effective workspace host-action capabilities
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess  # noqa: S404 - fixed no-shell container runtime argv below.
import sys
import urllib.parse
import urllib.request
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path

from pynchy.personalization_cli import publish_personalization, validate_personalization
from pynchy.remote_ops import RemoteOpsError, run_remote_op

_DEFAULT_PORT = "8484"
_DEFAULT_HOST = f"localhost:{_DEFAULT_PORT}"
_DEFAULT_CONTROL_SOCKET = Path("data/pynchy.sock")
_DEFAULT_CONTROL_TOKEN_FILE = Path("data/control-plane.token")
_DEFAULT_CONTROL_TOKEN_ENV = "PYNCHY_CONTROL_TOKEN"  # noqa: S105 - environment variable name, not a credential value.


def _stdout_line(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _stderr_line(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def _run() -> None:
    from dotenv import (  # noqa: PLC0415 - CLI entrypoint keeps heavy app imports lazy.
        load_dotenv,
    )

    load_dotenv()  # Materialize host credentials for explicitly declared tool access.

    from pynchy.config.api import (  # noqa: PLC0415 - startup validates composed settings before app construction.
        PersonalizationError,
        validate_personalization_configuration,
    )

    try:
        validate_personalization_configuration(Path.cwd(), Path("data/personalization"))
    except (OSError, PersonalizationError, ValueError) as exc:
        _stderr_line(f"Personalization validation failed: {exc}")
        sys.exit(2)

    from pynchy.logger import (  # noqa: PLC0415 - configure the error log before application startup.
        configure_error_log,
    )

    configure_error_log(Path("logs/pynchy.error.log"))

    from pynchy.host.orchestrator.app import (  # noqa: PLC0415 - avoid importing the orchestrator for --help and other subcommands.
        PynchyApp,
    )

    app = PynchyApp()
    asyncio.run(app.run())


def whatsapp_auth() -> None:  # noqa: V103
    """Run the WhatsApp QR login with the configured credential database."""
    from pynchy.config.api import (  # noqa: PLC0415 - CLI composition resolves the auth database path.
        get_settings,
    )
    from pynchy.plugins.channels.whatsapp.auth import (  # noqa: PLC0415 - QR support is only needed for this command.
        main as authenticate,
    )

    data_dir = get_settings().data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    authenticate(str(data_dir / "neonize.db"))


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
    from pynchy.host.orchestrator.http_control import (  # noqa: PLC0415 - client auth is only needed for control-plane commands.
        load_control_plane_client_token,
    )

    return load_control_plane_client_token(
        token_env=_DEFAULT_CONTROL_TOKEN_ENV,
        token_file=token_file or _DEFAULT_CONTROL_TOKEN_FILE,
    )


def _build() -> None:
    from pynchy.config.api import (  # noqa: PLC0415 - build command loads settings only when invoked.
        get_settings,
    )
    from pynchy.plugins.runtimes.apple_build_lock import (  # noqa: PLC0415 - build command needs Apple-only lock.
        apple_build_lock,
    )
    from pynchy.plugins.runtimes.cleanup import (  # noqa: PLC0415 - runtime cleanup is build-command specific.
        cleanup_runtime_build_state,
        cleanup_runtime_builder,
    )
    from pynchy.plugins.runtimes.detection import (  # noqa: PLC0415 - runtime probing is build-command specific.
        configure_runtime_override,
        get_runtime,
    )

    s = get_settings()
    configure_runtime_override(s.container.runtime)
    runtime = get_runtime()
    container_dir = s.project_root / "src" / "pynchy" / "agent"

    if not (container_dir / "Dockerfile").exists():
        _stderr_line(f"Error: No Dockerfile at {container_dir / 'Dockerfile'}")
        sys.exit(1)

    _stdout_line(f"Building {s.container.image} with {runtime.cli}...")
    runtime.ensure_running()
    lock = apple_build_lock() if runtime.cli == "container" else nullcontext()
    with lock:
        if not cleanup_runtime_build_state(runtime):
            _stderr_line("Error: Could not clean stale container build state")
            sys.exit(1)
        build_state_cleaned = False
        try:
            result = subprocess.run(  # noqa: S603 - runtime CLI is selected by trusted runtime detection and argv is fixed.
                [runtime.cli, "build", "-t", s.container.image, "."],
                cwd=str(container_dir),
                check=False,
            )
        finally:
            build_state_cleaned = cleanup_runtime_build_state(runtime)
        if result.returncode != 0:
            cleanup_runtime_builder(runtime)
    if result.returncode == 0 and not build_state_cleaned:
        _stderr_line("Error: Could not clean container build state after the build")
        sys.exit(1)
    sys.exit(result.returncode)


def _bootstrap_control_plane_token(*, rotate: bool) -> int:
    from pynchy.config.api import (  # noqa: PLC0415 - bootstrap reads settings only for this subcommand.
        get_settings,
    )
    from pynchy.host.orchestrator.http_control import (  # noqa: PLC0415 - token creation is isolated to the control-plane command.
        ControlPlaneConfigurationError,
        bootstrap_control_plane_token,
    )

    settings = get_settings()
    try:
        path = bootstrap_control_plane_token(
            auth_token_file=settings.server.auth_token_file,
            project_root=settings.project_root,
            rotate=rotate,
        )
    except (ControlPlaneConfigurationError, OSError) as exc:
        _stderr_line(f"Control-plane bootstrap failed: {exc}")
        return 1
    action = "Rotated" if rotate else "Created"
    _stdout_line(f"{action} permission-restricted control-plane token: {path}")
    return 0


def _control_url(host: str, relative_url: str) -> str:
    base = host if "://" in host else f"http://{host}"
    return f"{base.rstrip('/')}/{relative_url.lstrip('/')}"


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
    method: str,
    bearer_token: str | None,
) -> object:
    import aiohttp  # noqa: PLC0415 - Unix HTTP client is only needed by local CLI control.

    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
    connector = aiohttp.UnixConnector(path=str(socket_path))
    try:
        async with (
            aiohttp.ClientSession(connector=connector, headers=headers) as session,
            session.request(method, f"http://localhost{relative_url}") as response,
        ):
            response.raise_for_status()
            return await response.json()
    except aiohttp.ClientError as exc:
        raise OSError(str(exc)) from exc


def _doctor(
    host: str | None,
    workspace: str | None,
    *,
    json_output: bool,
    socket_path: Path | None = None,
    token_file: Path | None = None,
) -> int:
    try:
        relative_url = "/capabilities"
        if workspace:
            relative_url += f"?{urllib.parse.urlencode({'workspace': workspace})}"
        payload = _fetch_control_payload(
            host,
            relative_url,
            method="GET",
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


def _fetch_control_payload(
    host: str | None,
    relative_url: str,
    *,
    method: str,
    socket_path: Path | None,
    token_file: Path | None,
) -> object:
    selected_host, selected_socket = _control_client_target(host, socket_path)
    token = _control_client_token(token_file)
    if selected_socket is not None:
        return asyncio.run(
            _read_unix_json(selected_socket, relative_url, method=method, bearer_token=token)
        )
    if selected_host is None:
        raise ValueError("No TCP host or Unix socket selected for the control-plane client")

    url = _control_url(selected_host, relative_url)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request: str | urllib.request.Request = url
    if token or method != "GET":
        request = urllib.request.Request(  # noqa: S310 - operator-selected endpoint with bearer auth.
            url,
            headers=headers,
            method=method,
        )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - operator-selected Pynchy status endpoint is intentionally queried.
        return json.loads(response.read())


def _control_command(
    relative_url: str,
    *,
    method: str,
    host: str | None,
    socket_path: Path | None,
    token_file: Path | None,
) -> int:
    try:
        payload = _fetch_control_payload(
            host,
            relative_url,
            method=method,
            socket_path=socket_path,
            token_file=token_file,
        )
    except (OSError, TypeError, ValueError) as exc:
        _stderr_line(f"{relative_url.removeprefix('/').capitalize()} failed: {exc}")
        return 1
    _stdout_line(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _status_summary(*, host: str | None, socket_path: Path | None, token_file: Path | None) -> int:
    try:
        payload = _fetch_control_payload(
            host,
            "/status?summary=1",
            method="GET",
            socket_path=socket_path,
            token_file=token_file,
        )
    except (OSError, TypeError, ValueError) as exc:
        _stderr_line(f"Status summary failed: {exc}")
        return 1
    _stdout_line(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


def _ops_command(operation: str) -> int:
    from pynchy.config.api import get_settings  # noqa: PLC0415 - private ops config is lazy.

    try:
        _stdout_line(run_remote_op(get_settings().ops, operation))
    except (OSError, subprocess.TimeoutExpired, RemoteOpsError) as exc:
        _stderr_line(f"Ops {operation} failed: {exc}")
        return 1
    return 0


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
    sub.add_parser(
        "publish-personalization",
        help=(
            "Validate and publish the canonical personalization repository from this host checkout"
        ),
    )
    status = sub.add_parser(
        "status", help="Read service status through the authenticated control plane"
    )
    status.add_argument(
        "--summary", action="store_true", help="Return bounded service, deploy, and queue state"
    )
    sub.add_parser("deploy", help="Request deployment through the authenticated control plane")
    validate_personalization_parser = sub.add_parser(
        "validate-personalization",
        help="Validate a personalization repository against this Pynchy checkout",
    )
    validate_personalization_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("data/personalization"),
        help="Repository path (default: data/personalization)",
    )
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
    ops = sub.add_parser("ops", help="Run one fixed read-only remote Kubernetes diagnostic")
    ops.add_argument("operation", choices=("status", "logs", "messages", "events"))

    args = parser.parse_args()

    match args.command:
        case "build":
            _build()
        case "publish-personalization":
            sys.exit(publish_personalization())
        case "status":
            sys.exit(
                _status_summary(host=args.host, socket_path=args.socket, token_file=args.token_file)
                if args.summary
                else _control_command(
                    "/status",
                    method="GET",
                    host=args.host,
                    socket_path=args.socket,
                    token_file=args.token_file,
                )
            )
        case "ops":
            sys.exit(_ops_command(args.operation))
        case "deploy":
            sys.exit(
                _control_command(
                    "/deploy",
                    method="POST",
                    host=args.host,
                    socket_path=args.socket,
                    token_file=args.token_file,
                )
            )
        case "validate-personalization":
            sys.exit(validate_personalization(args.path))
        case "control-plane":
            sys.exit(_bootstrap_control_plane_token(rotate=args.rotate))
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
