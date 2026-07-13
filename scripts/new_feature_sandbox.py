#!/usr/bin/env python3
"""Provision and manage a complete runtime for a new-feature worktree."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess  # noqa: S404, RUF100 - commands are fixed lifecycle executables.
import sys
import time
import urllib.request
from pathlib import Path

import aiosqlite
from dotenv import dotenv_values, load_dotenv

from pynchy.plugins.memory.sqlite_memory.backend import SqliteMemoryBackend
from pynchy.state.schema import create_schema

_STATE_PATH = Path("data/new-feature/runtime.json")
_LOG_DIR = Path("logs/new-feature")
_ENV_REF = re.compile(r"os\.environ/(\w+)")
_SANDBOX_NAMESPACE = re.compile(r"pynchy[-_][a-z0-9][a-z0-9_.-]{0,55}")
_START_TIMEOUT_SECONDS = 120
_STOP_TIMEOUT_SECONDS = 35


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"new-feature did not provide required environment variable {name}")
    return value


def _port(name: str) -> int:
    value = int(_required_env(name))
    if not 1024 <= value <= 65535:
        raise RuntimeError(f"{name} must be an unprivileged TCP port")
    return value


def _namespace() -> str:
    namespace = _required_env("PYNCHY_RUNTIME_NAMESPACE")
    if not _SANDBOX_NAMESPACE.fullmatch(namespace):
        raise RuntimeError("Refusing to manage a runtime outside a pynchy-* sandbox namespace")
    return namespace


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _render_dotenv(source_root: Path, litellm_text: str) -> str:
    source_values = dotenv_values(source_root / ".env")
    values = {key: value for key, value in source_values.items() if value is not None}
    values.update(os.environ)
    forwarded = {
        name: values[name]
        for name in sorted(set(_ENV_REF.findall(litellm_text)))
        if values.get(name)
    }
    runtime_values = {
        "GATEWAY__MASTER_KEY": f"sk-{secrets.token_urlsafe(32)}",
        "GATEWAY__PORT": _required_env("GATEWAY__PORT"),
        "NEW_FEATURE_REPO_ROOT": _required_env("NEW_FEATURE_REPO_ROOT"),
        "NEW_FEATURE_TEMPORAL_PORT": _required_env("NEW_FEATURE_TEMPORAL_PORT"),
        "PYNCHY_DISABLE_SERVICE_INSTALL": "1",
        "PYNCHY_RUNTIME_NAMESPACE": _namespace(),
        "SERVER__PORT": _required_env("SERVER__PORT"),
    }
    forwarded.update(runtime_values)
    return "".join(f"{key}={json.dumps(value)}\n" for key, value in sorted(forwarded.items()))


def _write_runtime_config(source_root: Path) -> dict[str, object]:
    source_litellm = source_root / "litellm_config.yaml"
    if not source_litellm.exists():
        raise RuntimeError(
            "The control checkout needs a local litellm_config.yaml before creating a sandbox"
        )
    litellm_text = source_litellm.read_text(encoding="utf-8")
    Path("litellm_config.yaml").write_text(litellm_text, encoding="utf-8")

    server_port = _port("SERVER__PORT")
    gateway_port = _port("GATEWAY__PORT")
    temporal_port = _port("NEW_FEATURE_TEMPORAL_PORT")
    config = (
        "[container]\n"
        'runtime = "docker"\n\n'
        "[server]\n"
        f"port = {server_port}\n\n"
        "[gateway]\n"
        f"port = {gateway_port}\n"
        'litellm_config = "litellm_config.yaml"\n\n'
        "[scheduler]\n"
        f'temporal_address = "127.0.0.1:{temporal_port}"\n'
        f'temporal_task_queue = "{_namespace()}-scheduler"\n'
    )
    Path("config.toml").write_text(config, encoding="utf-8")
    _write_private(Path(".env"), _render_dotenv(source_root, litellm_text))
    return {
        "namespace": _namespace(),
        "server_port": server_port,
        "gateway_port": gateway_port,
        "temporal_port": temporal_port,
    }


async def _initialize_databases() -> None:
    data_dir = Path("data")
    async with aiosqlite.connect(data_dir / "messages.db") as database:
        await create_schema(database)
    memory = SqliteMemoryBackend()
    await memory.init()
    await memory.close()


def _write_state(state: dict[str, object]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_state() -> dict[str, object] | None:
    if not _STATE_PATH.exists():
        return None
    value = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Invalid sandbox state in {_STATE_PATH}")
    return value


def _start_process(command: list[str], log_name: str) -> subprocess.Popen[bytes]:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (_LOG_DIR / log_name).open("ab")
    return subprocess.Popen(  # noqa: S603, RUF100 - argv comes from fixed lifecycle commands.
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required sandbox executable is not available on PATH: {name}")
    return path


def _wait_for_port(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Process exited during startup; inspect {_LOG_DIR}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"Port {port} did not become ready; inspect {_LOG_DIR}")


def _wait_for_http(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}/status"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Pynchy exited during startup; inspect {_LOG_DIR / 'pynchy.log'}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310, RUF100 - fixed loopback URL.
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"Pynchy did not become ready at {url}; inspect {_LOG_DIR}")


def _stop_pid(pid: object) -> None:
    if not isinstance(pid, int) or pid <= 1:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.25)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _remove_runtime_resources(namespace: object) -> None:
    if not isinstance(namespace, str) or not _SANDBOX_NAMESPACE.fullmatch(namespace):
        return
    docker = shutil.which("docker")
    if docker is None:
        return
    listed = subprocess.run(  # noqa: S603, RUF100 - validated sandbox prefix only.
        [docker, "ps", "-aq", "--filter", f"name=^/{namespace}-"],
        capture_output=True,
        text=True,
        check=False,
    )
    for name in listed.stdout.splitlines():
        subprocess.run(  # noqa: S603, RUF100 - validated sandbox resource names only.
            [docker, "rm", "-f", name], capture_output=True, check=False
        )
    subprocess.run(  # noqa: S603, RUF100 - validated sandbox resource names only.
        [docker, "network", "rm", f"{namespace}-litellm-net"],
        capture_output=True,
        check=False,
    )
    subprocess.run(  # noqa: S603, RUF100 - validated sandbox resource names only.
        [docker, "volume", "rm", f"{namespace}-litellm-db-data"],
        capture_output=True,
        check=False,
    )


def stop() -> None:
    state = _read_state()
    if state is None:
        return
    _stop_pid(state.get("pynchy_pid"))
    _stop_pid(state.get("temporal_pid"))
    _remove_runtime_resources(state.get("namespace"))
    _STATE_PATH.unlink(missing_ok=True)


def _start_pynchy_runtime(state: dict[str, object], temporal: subprocess.Popen[bytes]) -> None:
    temporal_port = state["temporal_port"]
    server_port = state["server_port"]
    if not isinstance(temporal_port, int) or not isinstance(server_port, int):
        raise TypeError("Sandbox state ports must be integers")
    _wait_for_port(temporal_port, temporal)
    pynchy = _start_process([_executable("uv"), "run", "pynchy"], "pynchy.log")
    state["pynchy_pid"] = pynchy.pid
    _write_state(state)
    _wait_for_http(server_port, pynchy)


def setup() -> None:
    if _read_state() is not None:
        raise RuntimeError("Sandbox runtime already exists; run the stop command first")
    source_root = Path(_required_env("NEW_FEATURE_REPO_ROOT")).resolve()
    state = _write_runtime_config(source_root)
    Path("data").mkdir(parents=True, exist_ok=True)
    asyncio.run(_initialize_databases())

    temporal = _start_process(
        [
            _executable("temporal"),
            "server",
            "start-dev",
            "--ip",
            "127.0.0.1",
            "--port",
            str(state["temporal_port"]),
            "--headless",
            "--db-filename",
            str(Path("data/temporal.db").resolve()),
            "--log-level",
            "warn",
        ],
        "temporal.log",
    )
    state["temporal_pid"] = temporal.pid
    _write_state(state)
    try:
        _start_pynchy_runtime(state, temporal)
    except Exception:
        stop()
        raise


def status() -> None:
    state = _read_state()
    if state is None:
        raise RuntimeError("Sandbox runtime is not running")
    sys.stdout.write(json.dumps(state, indent=2, sort_keys=True) + "\n")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "stop", "restart", "status"))
    args = parser.parse_args()
    if args.command == "setup":
        setup()
    elif args.command == "stop":
        stop()
    elif args.command == "restart":
        stop()
        setup()
    else:
        status()


if __name__ == "__main__":
    main()
