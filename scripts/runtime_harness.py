#!/usr/bin/env python3
"""Run a hermetic, deterministic Pynchy runtime for development and CI."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess  # noqa: S404 - fixed lifecycle commands launch local runtime processes.
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from pynchy.host.container_manager import reaper
from pynchy.state.schema import create_schema

if TYPE_CHECKING:
    from collections.abc import Callable

_STATE_RELATIVE_PATH = Path("data/pynchy-runtime/runtime.json")
_LOG_RELATIVE_PATH = Path("logs/pynchy-runtime")
_HOME_RELATIVE_PATH = Path("data/pynchy-runtime/home")
_PERSONALIZATION_DIR = Path("data/personalization")
_CONFIG_FILE = "pynchy.toml"
_DOTENV_FILE = ".env"
_LITELLM_CONFIG_FILE = "litellm.yaml"
_RUNTIME_MARKER = "PYNCHY_RUNTIME_HARNESS"
_STATE_VERSION = 2
_START_TIMEOUT_SECONDS = 120
_STOP_TIMEOUT_SECONDS = 35
_FAKE_OPENAI_PORT = 8080
_DETERMINISTIC_MODEL = "pynchy-deterministic"
_DETERMINISTIC_RESPONSE = "Pynchy deterministic response."
_RUNTIME_AGENT_IMAGE_REPOSITORY = "pynchy-runtime-agent"
_RUNTIME_AGENT_SOURCE_LABEL = "io.pynchy.runtime-agent-source-sha256"
_RUNTIME_AGENT_IMAGE_DIGEST_LENGTH = 16
_RUNTIME_AGENT_BASE_IMAGE = (
    "python:3.13.12-slim-bookworm@"
    "sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6"
)
_RUNTIME_AGENT_UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.11.14@"
    "sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97"
)
_RUNTIME_AGENT_ROOT = Path("src/pynchy/agent")
_RUNTIME_AGENT_DOCKERFILE = _RUNTIME_AGENT_ROOT / "runtime.Dockerfile"
_RUNTIME_AGENT_DOCKERIGNORE = _RUNTIME_AGENT_ROOT / ".dockerignore"
_RUNTIME_AGENT_ENTRYPOINT = _RUNTIME_AGENT_ROOT / "runtime_entrypoint.sh"
_RUNTIME_AGENT_RUNNER_ROOT = _RUNTIME_AGENT_ROOT / "agent_runner"
_LITELLM_IMAGE = (
    "ghcr.io/berriai/litellm@"
    "sha256:9c1f1889774a973ce650f712ace6753a9b6dd1182d25d837b858dbcac6ea3056"
)
_POSTGRES_IMAGE = "postgres@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"
_SANDBOX_NAMESPACE = re.compile(r"pynchy[-_][a-z0-9][a-z0-9_.-]{0,55}")
_PROCESS_MARKER = re.compile(r"pynchy-runtime-(?:pynchy|temporal)-[0-9a-f]{32}")
_PROCESS_SUPERVISOR_SCRIPT = '"$@" &\nchild=$!\nwait "$child"\n'
_PROCESS_PID_KEYS = ("pynchy_pid", "temporal_pid")
_FAILED_SETUP_ARCHIVE_LIMIT = 5
_FEATURE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
_RUNTIME_DATABASE_NAMES = ("messages.db", "temporal.db")
_RUNTIME_CONTAINER_SUFFIXES = (
    "pynchy",
    "litellm",
    "litellm-db",
    "deterministic-openai",
)
_SYSTEM_ENV_ALLOWLIST = frozenset(
    {
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "UV_LINK_MODE",
    }
)


@dataclass(frozen=True)
class RuntimeSpec:
    """Explicit runtime allocation shared by new-feature and CI callers."""

    root: Path
    namespace: str
    server_port: int
    gateway_port: int
    temporal_port: int

    @property
    def state_path(self) -> Path:
        return self.root / _STATE_RELATIVE_PATH

    @property
    def log_dir(self) -> Path:
        return self.root / _LOG_RELATIVE_PATH

    @property
    def home_dir(self) -> Path:
        return self.root / _HOME_RELATIVE_PATH

    @property
    def fake_container_name(self) -> str:
        return f"{self.namespace}-deterministic-openai"

    @property
    def network_name(self) -> str:
        return f"{self.namespace}-litellm-net"


def _runtime_agent_source_paths(spec: RuntimeSpec) -> tuple[Path, ...]:
    runner_root = spec.root / _RUNTIME_AGENT_RUNNER_ROOT
    source_root = runner_root / "src"
    static_paths = (
        spec.root / _RUNTIME_AGENT_DOCKERFILE,
        spec.root / _RUNTIME_AGENT_DOCKERIGNORE,
        spec.root / _RUNTIME_AGENT_ENTRYPOINT,
        runner_root / "pyproject.toml",
        runner_root / "uv.lock",
    )
    if not source_root.is_dir():
        raise RuntimeError(f"Deterministic runtime agent source is missing: {source_root}")
    source_paths = sorted(path for path in source_root.rglob("*") if _is_runtime_agent_source(path))
    return (*static_paths, *source_paths)


def _is_runtime_agent_source(path: Path) -> bool:
    """Exclude local Python caches from the image identity."""
    return (
        path.is_file() and path.suffix not in {".pyc", ".pyo"} and "__pycache__" not in path.parts
    )


def _runtime_agent_source_digest(spec: RuntimeSpec) -> str:
    """Fingerprint the exact non-transient inputs to the test-agent build."""
    digest = hashlib.sha256()
    for source_path in _runtime_agent_source_paths(spec):
        if not source_path.is_file():
            raise RuntimeError(f"Deterministic runtime agent source is missing: {source_path}")
        digest.update(source_path.relative_to(spec.root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _runtime_agent_image(spec: RuntimeSpec) -> str:
    """Name the test image from its isolated namespace and source digest."""
    digest = _runtime_agent_source_digest(spec)[:_RUNTIME_AGENT_IMAGE_DIGEST_LENGTH]
    return f"{_RUNTIME_AGENT_IMAGE_REPOSITORY}:{spec.namespace}-{digest}"


def _port(name: str, value: int) -> int:
    if not 1024 <= value <= 65535:
        raise RuntimeError(f"{name} must be an unprivileged TCP port")
    return value


def _available_port(excluded: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            candidate = _port("auto-allocated port", sock.getsockname()[1])
        if candidate not in excluded:
            return candidate


def _namespace(value: str) -> str:
    if not _SANDBOX_NAMESPACE.fullmatch(value):
        raise RuntimeError("Runtime namespace must stay inside a pynchy-* sandbox namespace")
    return value


def _runtime_spec(args: argparse.Namespace) -> RuntimeSpec:
    root = args.root.expanduser().resolve()
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError(f"Runtime root must be a Pynchy checkout: {root}")

    namespace = args.namespace or os.environ.get("PYNCHY_RUNTIME_NAMESPACE")
    if not namespace:
        namespace = f"pynchy-runtime-{secrets.token_hex(6)}"

    server_port = _resolve_port(args.server_port, "SERVER__PORT", set())
    gateway_port = _resolve_port(args.gateway_port, "GATEWAY__PORT", {server_port})
    temporal_port = _resolve_port(
        args.temporal_port,
        "NEW_FEATURE_TEMPORAL_PORT",
        {server_port, gateway_port},
    )
    if len({server_port, gateway_port, temporal_port}) != 3:
        raise RuntimeError("Runtime server, gateway, and Temporal ports must be distinct")

    return RuntimeSpec(
        root=root,
        namespace=_namespace(namespace),
        server_port=server_port,
        gateway_port=gateway_port,
        temporal_port=temporal_port,
    )


def _resolve_port(argument_value: int | None, env_name: str, excluded: set[int]) -> int:
    if argument_value is not None:
        return _port(env_name, argument_value)
    if env_value := os.environ.get(env_name):
        try:
            return _port(env_name, int(env_value))
        except ValueError as exc:
            raise RuntimeError(f"{env_name} must be an integer TCP port") from exc
    return _available_port(excluded)


def _spec_from_state(root: Path, state: dict[str, object]) -> RuntimeSpec:
    namespace = state.get("namespace")
    server_port = state.get("server_port")
    gateway_port = state.get("gateway_port")
    temporal_port = state.get("temporal_port")
    if not isinstance(namespace, str):
        raise TypeError("Runtime state namespace must be a string")
    if (
        not isinstance(server_port, int)
        or not isinstance(gateway_port, int)
        or not isinstance(temporal_port, int)
    ):
        raise TypeError("Runtime state ports must be integers")
    return RuntimeSpec(
        root=root,
        namespace=_namespace(namespace),
        server_port=_port("server_port", server_port),
        gateway_port=_port("gateway_port", gateway_port),
        temporal_port=_port("temporal_port", temporal_port),
    )


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _generated_dotenv(spec: RuntimeSpec, gateway_key: str) -> str:
    values = {
        "GATEWAY__MASTER_KEY": gateway_key,
        "PYNCHY_DETERMINISTIC_API_KEY": "deterministic",  # pragma: allowlist secret
        "PYNCHY_DISABLE_SERVICE_INSTALL": "1",
        "PYNCHY_RUNTIME_HARNESS": "1",
        "PYNCHY_RUNTIME_NAMESPACE": spec.namespace,
    }
    return "".join(f"{key}={json.dumps(value)}\n" for key, value in sorted(values.items()))


def _generated_litellm_config(spec: RuntimeSpec) -> str:
    return (
        "# Generated by scripts/runtime_harness.py. Do not add provider routes here.\n"
        "model_list:\n"
        f"  - model_name: {_DETERMINISTIC_MODEL}\n"
        "    litellm_params:\n"
        f"      model: openai/{_DETERMINISTIC_MODEL}\n"
        f"      api_base: http://{spec.fake_container_name}:{_FAKE_OPENAI_PORT}/v1\n"
        "      api_key: os.environ/PYNCHY_DETERMINISTIC_API_KEY\n"
        "    model_info:\n"
        "      id: pynchy-deterministic\n"
        "      mode: responses\n"
        "\n"
        "router_settings:\n"
        "  num_retries: 0\n"
        "\n"
        "general_settings:\n"
        "  master_key: os.environ/LITELLM_MASTER_KEY\n"
    )


def _generated_config(spec: RuntimeSpec) -> str:
    agent_image = _runtime_agent_image(spec)
    return (
        "# Generated by scripts/runtime_harness.py.\n"
        "[agent]\n"
        'default_core = "openai"\n'
        f"model = {json.dumps(_DETERMINISTIC_MODEL)}\n\n"
        "[container]\n"
        f"image = {json.dumps(agent_image)}\n"
        'runtime = "docker"\n\n'
        "[server]\n"
        f"port = {spec.server_port}\n\n"
        "[gateway]\n"
        f"port = {spec.gateway_port}\n"
        f"litellm_image = {json.dumps(_LITELLM_IMAGE)}\n"
        f"postgres_image = {json.dumps(_POSTGRES_IMAGE)}\n\n"
        "[scheduler]\n"
        f'temporal_address = "127.0.0.1:{spec.temporal_port}"\n'
        f"temporal_task_queue = {json.dumps(f'{spec.namespace}-scheduler')}\n"
        "\n[workspaces.pynchy]\n"
    )


def _ensure_safe_runtime_root(spec: RuntimeSpec) -> None:
    if spec.state_path.exists():
        return
    dotenv_path = spec.root / _DOTENV_FILE
    if dotenv_path.exists() and _RUNTIME_MARKER not in dotenv_path.read_text(encoding="utf-8"):
        raise RuntimeError(
            "Refusing to replace an existing .env; run the harness from an isolated worktree"
        )
    for filename in (_CONFIG_FILE, _LITELLM_CONFIG_FILE):
        path = spec.root / _PERSONALIZATION_DIR / filename
        if path.exists() and "Generated by scripts/runtime_harness.py" not in path.read_text(
            encoding="utf-8"
        ):
            raise RuntimeError(
                f"Refusing to replace existing {filename}; "
                "run the harness from an isolated worktree"
            )


def _write_runtime_config(spec: RuntimeSpec) -> dict[str, object]:
    _ensure_safe_runtime_root(spec)
    gateway_key = f"sk-{secrets.token_urlsafe(32)}"
    personalization = spec.root / _PERSONALIZATION_DIR
    personalization.mkdir(parents=True, exist_ok=True)
    (personalization / _LITELLM_CONFIG_FILE).write_text(
        _generated_litellm_config(spec),
        encoding="utf-8",
    )
    (personalization / _CONFIG_FILE).write_text(_generated_config(spec), encoding="utf-8")
    _write_private(spec.root / _DOTENV_FILE, _generated_dotenv(spec, gateway_key))
    return {
        "version": _STATE_VERSION,
        "namespace": spec.namespace,
        "server_port": spec.server_port,
        "gateway_port": spec.gateway_port,
        "temporal_port": spec.temporal_port,
        "gateway_key": gateway_key,
        "server_url": f"http://127.0.0.1:{spec.server_port}",
        "gateway_url": f"http://127.0.0.1:{spec.gateway_port}",
        "database_path": str(spec.root / "data" / "messages.db"),
        "fake_container": spec.fake_container_name,
        "network": spec.network_name,
        "model": _DETERMINISTIC_MODEL,
        "response_text": _DETERMINISTIC_RESPONSE,
        "agent_image": _runtime_agent_image(spec),
        "agent_source_digest": _runtime_agent_source_digest(spec),
    }


async def _initialize_databases(root: Path) -> None:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(data_dir / "messages.db") as database:
        await create_schema(database)
        await database.execute(
            "INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
            ("runtime:pynchy", "Pynchy", "1970-01-01T00:00:00+00:00"),
        )
        await database.execute(
            """
            INSERT OR REPLACE INTO registered_groups
                (jid, name, folder, trigger_pattern, added_at,
                 container_config, security_profile, is_admin)
            VALUES (?, ?, ?, ?, ?, NULL, ?, 1)
            """,
            (
                "runtime:pynchy",
                "Pynchy",
                "pynchy",
                "@pynchy",
                "1970-01-01T00:00:00+00:00",
                json.dumps({"services": {}, "contains_secrets": False}),
            ),
        )
        await database.commit()


def _reset_runtime_databases(root: Path) -> None:
    """Remove only SQLite state owned by a fresh deterministic runtime."""
    data_dir = root / "data"
    for name in _RUNTIME_DATABASE_NAMES:
        database = data_dir / name
        paths = (database, database.with_name(f"{name}-shm"), database.with_name(f"{name}-wal"))
        for path in paths:
            path.unlink(missing_ok=True)


def _write_state(spec: RuntimeSpec, state: dict[str, object]) -> None:
    _write_private(spec.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _read_state(root: Path) -> dict[str, object] | None:
    state_path = root / _STATE_RELATIVE_PATH
    if not state_path.exists():
        return None
    value = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Invalid runtime state in {state_path}")
    return value


def _redacted_state(state: dict[str, object]) -> dict[str, object]:
    """Return runtime diagnostics without exposing its loopback gateway credential."""
    visible_state = dict(state)
    if "gateway_key" in visible_state:
        visible_state["gateway_key"] = "<redacted>"
    return visible_state


def _runtime_environment(spec: RuntimeSpec, state: dict[str, object]) -> dict[str, str]:
    gateway_key = state.get("gateway_key")
    if not isinstance(gateway_key, str):
        raise TypeError("Runtime state gateway_key must be a string")
    home_dir = spec.home_dir
    xdg_cache_home = home_dir / "cache"
    xdg_config_home = home_dir / "config"
    xdg_data_home = home_dir / "data"
    xdg_runtime_dir = home_dir / "runtime"
    temp_dir = home_dir / "tmp"
    uv_cache_dir = home_dir / "uv-cache"
    for directory in (
        home_dir,
        xdg_cache_home,
        xdg_config_home,
        xdg_data_home,
        xdg_runtime_dir,
        temp_dir,
        uv_cache_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    environment = {name: value for name in _SYSTEM_ENV_ALLOWLIST if (value := os.environ.get(name))}
    # Developers and CI share this profile specifically so ambient provider and
    # channel credentials never influence either runtime.
    environment.update(
        {
            "GATEWAY__MASTER_KEY": gateway_key,
            "HOME": str(home_dir),
            "PYNCHY_DISABLE_SERVICE_INSTALL": "1",
            "PYNCHY_RUNTIME_HARNESS": "1",
            "PYNCHY_RUNTIME_NAMESPACE": spec.namespace,
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
            "TMPDIR": str(temp_dir),
            "UV_CACHE_DIR": str(uv_cache_dir),
            "XDG_CACHE_HOME": str(xdg_cache_home),
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "XDG_DATA_HOME": str(xdg_data_home),
            "XDG_RUNTIME_DIR": str(xdg_runtime_dir),
        }
    )
    return environment


def _test_environment(spec: RuntimeSpec, state: dict[str, object]) -> dict[str, str]:
    environment = _runtime_environment(spec, state)
    environment.update(
        {
            "PYNCHY_RUNTIME_STATE": str(spec.state_path),
            "PYNCHY_RUNTIME_URL": f"http://127.0.0.1:{spec.server_port}",
            "PYNCHY_RUNTIME_GATEWAY_URL": f"http://127.0.0.1:{spec.gateway_port}",
        }
    )
    return environment


def _start_process(
    command: list[str],
    log_path: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    process_marker: str,
) -> subprocess.Popen[bytes]:
    """Start one process-group leader whose identity survives a later stop command."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        # The static shell waits for the real child instead of exec'ing it, so
        # its unique ``$0`` marker remains visible in ``ps``. A persisted PID
        # alone becomes unsafe after the operating system reuses it.
        return subprocess.Popen(  # noqa: S603 - fixed supervisor script and local lifecycle argv.
            [_executable("sh"), "-c", _PROCESS_SUPERVISOR_SCRIPT, process_marker, *command],
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required runtime executable is not available on PATH: {name}")
    return path


def _run_docker(
    docker: str,
    *args: str,
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - Docker argv is assembled from validated runtime names.
        [docker, *args],
        capture_output=True,
        check=check,
        text=True,
        timeout=timeout,
    )


def _ensure_docker_network(docker: str, network_name: str, labels: list[str]) -> None:
    result = _run_docker(docker, "network", "inspect", network_name, check=False)
    if result.returncode == 0:
        return
    _run_docker(docker, "network", "create", *labels, network_name)


def _ensure_docker_image(docker: str, image: str) -> None:
    result = _run_docker(docker, "image", "inspect", image, check=False)
    if result.returncode == 0:
        return
    _run_docker(docker, "pull", image)


def _provenance_labels(spec: RuntimeSpec) -> list[str]:
    """Stamp test provenance so abandoned resources stay identifiable."""
    return reaper.provenance_label_args(
        namespace=spec.namespace,
        pid=os.getpid(),
        boot_id=reaper.current_boot_id(),
    )


def _start_fake_openai(spec: RuntimeSpec, state: dict[str, object]) -> None:
    docker = _executable("docker")
    server_path = spec.root / "scripts" / "deterministic_openai_server.py"
    if not server_path.is_file():
        raise RuntimeError(f"Deterministic OpenAI server is missing: {server_path}")
    labels = _provenance_labels(spec)
    _ensure_docker_network(docker, spec.network_name, labels)
    _ensure_docker_image(docker, _LITELLM_IMAGE)
    _run_docker(docker, "rm", "-f", spec.fake_container_name, check=False)
    _run_docker(
        docker,
        "run",
        "-d",
        "--init",
        "--name",
        spec.fake_container_name,
        "--network",
        spec.network_name,
        "--restart",
        "no",
        *labels,
        "-v",
        f"{server_path}:/runtime/deterministic_openai_server.py:ro",
        "-e",
        f"PYNCHY_DETERMINISTIC_RESPONSE={_DETERMINISTIC_RESPONSE}",
        "--entrypoint",
        "python",
        _LITELLM_IMAGE,
        "/runtime/deterministic_openai_server.py",
        "--port",
        str(_FAKE_OPENAI_PORT),
    )
    _wait_for_fake_openai(docker, spec.fake_container_name)
    state["fake_container"] = spec.fake_container_name
    _write_state(spec, state)


def _ensure_runtime_agent_image(spec: RuntimeSpec, state: dict[str, object]) -> None:
    """Build the locked real agent runner from its deterministic inputs.

    The production agent Dockerfile intentionally installs mutable external
    tooling. This image retains the real runner, OpenAI SDK, and IPC loop while
    leaving those mutable CLIs and plugins out of deterministic CI.
    """
    image = state.get("agent_image")
    if not isinstance(image, str):
        raise TypeError("Runtime state agent_image must be a string")
    source_digest = _runtime_agent_source_digest(spec)
    dockerfile = spec.root / _RUNTIME_AGENT_DOCKERFILE
    docker = _executable("docker")
    _ensure_docker_image(docker, _RUNTIME_AGENT_BASE_IMAGE)
    _ensure_docker_image(docker, _RUNTIME_AGENT_UV_IMAGE)
    _run_docker(
        docker,
        "build",
        "--pull=false",
        "--tag",
        image,
        "--label",
        f"{_RUNTIME_AGENT_SOURCE_LABEL}={source_digest}",
        "--file",
        str(dockerfile),
        str(spec.root / _RUNTIME_AGENT_ROOT),
    )


def _wait_for_fake_openai(docker: str, container_name: str) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    health_probe = (
        "from urllib.request import urlopen; "
        "urlopen('http://127.0.0.1:8080/healthz', timeout=1).read()"
    )
    while time.monotonic() < deadline:
        result = _run_docker(
            docker,
            "exec",
            container_name,
            "python",
            "-c",
            health_probe,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            return
        running = _run_docker(
            docker,
            "inspect",
            "-f",
            "{{.State.Running}}",
            container_name,
            check=False,
            timeout=5,
        )
        if running.stdout.strip() != "true":
            logs = _run_docker(docker, "logs", "--tail", "30", container_name, check=False)
            raise RuntimeError(f"Deterministic OpenAI sidecar exited: {logs.stdout[-2000:]}")
        time.sleep(0.25)
    raise TimeoutError("Deterministic OpenAI sidecar did not become ready")


def _wait_for_port(port: int, process: subprocess.Popen[bytes], log_dir: Path) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Process exited during startup; inspect {log_dir}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"Port {port} did not become ready; inspect {log_dir}")


def is_runtime_ready(status: object) -> bool:
    """Return whether a runtime status response proves every service is usable."""
    if not isinstance(status, dict):
        return False
    service = status.get("service")
    gateway = status.get("gateway")
    temporal = status.get("temporal")
    if not all(isinstance(value, dict) for value in (service, gateway, temporal)):
        return False
    return (
        service.get("status") == "ok"
        and gateway.get("litellm_container") == "running"
        and gateway.get("postgres_container") == "running"
        and gateway.get("ready") is True
        and gateway.get("database") == "connected"
        and temporal.get("cluster_healthy") is True
        and temporal.get("worker_running") is True
    )


def _wait_for_runtime(spec: RuntimeSpec, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{spec.server_port}/status"
    last_status: object = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Pynchy exited during startup; inspect {spec.log_dir / 'pynchy.general.log'}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # fixed loopback status URL.
                if response.status == 200:
                    last_status = json.loads(response.read())
                    if is_runtime_ready(last_status):
                        return
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    raise TimeoutError(
        f"Pynchy did not become semantically ready at {url}; last status: {last_status!r}; "
        f"inspect {spec.log_dir}"
    )


def _new_process_marker(name: str) -> str:
    if name not in {"pynchy", "temporal"}:
        raise ValueError(f"Unsupported runtime process name: {name}")
    return f"pynchy-runtime-{name}-{secrets.token_hex(16)}"


def _process_has_marker(pid: int, marker: str) -> bool:
    """Check that a persisted PID still belongs to this harness process group."""
    if not _PROCESS_MARKER.fullmatch(marker):
        return False
    ps = shutil.which("ps")
    if ps is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - ps receives a validated PID and marker lookup only.
            [ps, "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and marker in result.stdout


def _stop_pid(
    pid: object,
    marker: object,
    *,
    after_term: Callable[[], None] | None = None,
) -> None:
    if (
        not isinstance(pid, int)
        or pid <= 1
        or not isinstance(marker, str)
        or not _process_has_marker(pid, marker)
    ):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if after_term is not None:
        after_term()
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        _reap_harness_child(pid)
        # The shell leader may exit before an uncooperative child. Signal-zero
        # checks the whole owned process group, not only the leader PID, before
        # deciding that teardown succeeded.
        if not _process_group_has_live_member(pid):
            _reap_harness_child(pid)
            return
        time.sleep(0.25)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    kill_deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < kill_deadline:
        _reap_harness_child(pid)
        if not _process_group_has_live_member(pid):
            _reap_harness_child(pid)
            return
        time.sleep(0.25)
    _reap_harness_child(pid)


def _process_group_has_live_member(process_group_id: int) -> bool:
    """Return whether an owned process group contains a non-zombie process.

    ``stop`` can run in a subprocess of the process that originally launched
    the supervisors. That subprocess cannot reap the supervisors' zombies,
    even though their real children are gone. ``killpg(..., 0)`` alone would
    therefore wait for the full timeout on an already-stopped runtime.
    """
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can deny a signal-zero probe for a group containing only zombies.
        # The status scan below still distinguishes it from a live owned group.
        pass
    ps = shutil.which("ps")
    if ps is None:
        return True
    try:
        result = subprocess.run(  # noqa: S603 - fixed local process-status probe for an owned group.
            [ps, "-eo", "pgid=,stat="],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode != 0:
        return True
    statuses = [
        fields[1]
        for line in result.stdout.splitlines()
        if len(fields := line.split()) == 2 and fields[0] == str(process_group_id)
    ]
    return not statuses or any(not status.startswith("Z") for status in statuses)


def _reap_harness_child(pid: int) -> None:
    """Reap a local direct child without mistaking its exit for group cleanup."""
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def _remove_runtime_resources(namespace: object) -> None:
    if not isinstance(namespace, str) or not _SANDBOX_NAMESPACE.fullmatch(namespace):
        return
    docker = shutil.which("docker")
    if docker is None:
        return
    # The deterministic profile has one interactive agent and three named
    # sidecars. Remove those exact names rather than a namespace prefix: a
    # valid longer namespace can itself begin with this namespace.
    for suffix in _RUNTIME_CONTAINER_SUFFIXES:
        _run_docker(docker, "rm", "-f", f"{namespace}-{suffix}", check=False, timeout=30)
    _run_docker(docker, "network", "rm", f"{namespace}-litellm-net", check=False, timeout=30)
    _run_docker(
        docker,
        "volume",
        "rm",
        f"{namespace}-litellm-db-data",
        check=False,
        timeout=30,
    )


def _remove_runtime_agent_container(namespace: object) -> None:
    """Force-stop the attached deterministic agent before waiting for Pynchy."""
    if not isinstance(namespace, str) or not _SANDBOX_NAMESPACE.fullmatch(namespace):
        return
    docker = shutil.which("docker")
    if docker is None:
        return
    _run_docker(docker, "rm", "-f", f"{namespace}-pynchy", check=False, timeout=30)


def _remove_runtime_agent_image(namespace: object, image: object) -> None:
    """Remove only the namespace-scoped image created by this stopped runtime."""
    if not isinstance(namespace, str) or not _SANDBOX_NAMESPACE.fullmatch(namespace):
        return
    image_pattern = re.compile(
        rf"{re.escape(f'{_RUNTIME_AGENT_IMAGE_REPOSITORY}:{namespace}')}-"
        rf"[0-9a-f]{{{_RUNTIME_AGENT_IMAGE_DIGEST_LENGTH}}}"
    )
    if not isinstance(image, str) or not image_pattern.fullmatch(image):
        return
    docker = shutil.which("docker")
    if docker is None:
        return
    _run_docker(docker, "image", "rm", image, check=False, timeout=30)


def stop(root: Path, *, preserve_state: bool = False) -> None:
    state = _read_state(root)
    if state is None:
        # Without state the namespace is unknown, so name-derived cleanup can
        # remove nothing. Provenance labels still identify what this host
        # abandoned, which is what stranded resources permanently before.
        reaper.reap_now()
        return
    _refuse_unverified_legacy_processes(state)
    namespace = state.get("namespace")
    _stop_pid(
        state.get("pynchy_pid"),
        state.get("pynchy_marker"),
        # Pynchy's interactive Docker client inherits the host process group
        # and can outlive its host parent. Removing this exact container makes
        # that attached client exit before the group teardown wait begins.
        after_term=lambda: _remove_runtime_agent_container(namespace),
    )
    _stop_pid(state.get("temporal_pid"), state.get("temporal_marker"))
    _remove_runtime_resources(namespace)
    _remove_runtime_agent_image(namespace, state.get("agent_image"))
    if preserve_state:
        # Diagnostic state survives a failed test command, but stale PIDs must
        # never be signalled during a later stop/restart after the OS reuses them.
        state.pop("pynchy_pid", None)
        state.pop("temporal_pid", None)
        state.pop("pynchy_marker", None)
        state.pop("temporal_marker", None)
        _write_private(
            root / _STATE_RELATIVE_PATH, json.dumps(state, indent=2, sort_keys=True) + "\n"
        )
        return
    (root / _STATE_RELATIVE_PATH).unlink(missing_ok=True)


def _refuse_unverified_legacy_processes(state: dict[str, object]) -> None:
    """Keep an upgraded harness from silently orphaning a v1 host process."""
    if state.get("version") == _STATE_VERSION:
        return
    if not any(isinstance(pid := state.get(key), int) and pid > 1 for key in _PROCESS_PID_KEYS):
        return
    raise RuntimeError(
        "Runtime state was created by an older harness and contains unverified host "
        "process PIDs. Stop it with that harness version (or stop its processes manually), "
        "then rerun this command."
    )


def _start_pynchy_runtime(
    spec: RuntimeSpec,
    state: dict[str, object],
    temporal: subprocess.Popen[bytes],
) -> None:
    _wait_for_port(spec.temporal_port, temporal, spec.log_dir)
    pynchy_marker = _new_process_marker("pynchy")
    pynchy = _start_process(
        [_executable("uv"), "run", "pynchy"],
        spec.log_dir / "pynchy.general.log",
        cwd=spec.root,
        env=_runtime_environment(spec, state),
        process_marker=pynchy_marker,
    )
    state["pynchy_pid"] = pynchy.pid
    state["pynchy_marker"] = pynchy_marker
    _write_state(spec, state)
    _wait_for_runtime(spec, pynchy)


def _initialize_runtime_data(spec: RuntimeSpec) -> None:
    with contextlib.chdir(spec.root):
        asyncio.run(_initialize_databases(spec.root))


def _start_runtime_services(spec: RuntimeSpec, state: dict[str, object]) -> None:
    _start_fake_openai(spec, state)
    _ensure_runtime_agent_image(spec, state)
    temporal_marker = _new_process_marker("temporal")
    temporal = _start_process(
        [
            _executable("temporal"),
            "--disable-config-env",
            "--disable-config-file",
            "server",
            "start-dev",
            "--ip",
            "127.0.0.1",
            "--port",
            str(spec.temporal_port),
            "--headless",
            "--db-filename",
            str((spec.root / "data" / "temporal.db").resolve()),
            "--log-level",
            "warn",
        ],
        spec.log_dir / "temporal.log",
        cwd=spec.root,
        env=_runtime_environment(spec, state),
        process_marker=temporal_marker,
    )
    state["temporal_pid"] = temporal.pid
    state["temporal_marker"] = temporal_marker
    _write_state(spec, state)
    _start_pynchy_runtime(spec, state, temporal)


def _archive_failed_new_feature_setup(spec: RuntimeSpec) -> Path | None:
    """Copy bounded runtime logs outside a worktree before new-feature removes it."""
    control_root_value = os.environ.get("NEW_FEATURE_REPO_ROOT")
    feature_slug = os.environ.get("NEW_FEATURE_SLUG")
    if (
        control_root_value is None
        or feature_slug is None
        or _FEATURE_SLUG.fullmatch(feature_slug) is None
        or not spec.log_dir.is_dir()
        or not any(path.is_file() for path in spec.log_dir.rglob("*"))
    ):
        return None

    control_root = Path(control_root_value).resolve()
    if not control_root.is_dir() or control_root == spec.root.resolve():
        return None

    archive_root = (
        control_root / ".new-feature" / "diagnostics" / "runtime-setup-failures" / feature_slug
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = archive_root / timestamp
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(spec.log_dir, destination)

    archives = sorted(path for path in archive_root.iterdir() if path.is_dir())
    for expired in archives[:-_FAILED_SETUP_ARCHIVE_LIMIT]:
        shutil.rmtree(expired)
    return destination


def setup(spec: RuntimeSpec, *, reset_data: bool = True) -> dict[str, object]:
    if _read_state(spec.root) is not None:
        raise RuntimeError("Runtime already exists; run the stop command first")
    if reset_data:
        _reset_runtime_databases(spec.root)
    state = _write_runtime_config(spec)
    _write_state(spec, state)
    try:
        _initialize_runtime_data(spec)
        _start_runtime_services(spec, state)
    # allow: exception-handling - partial startup must remove every owned runtime resource.
    except Exception as error:
        archive_path: Path | None = None
        with contextlib.suppress(OSError):
            archive_path = _archive_failed_new_feature_setup(spec)
        stop(spec.root)
        if archive_path is not None:
            error.add_note(f"Runtime setup logs preserved at {archive_path}")
        raise
    return state


def restart(root: Path, args: argparse.Namespace) -> None:
    previous = _read_state(root)
    if previous is None:
        setup(_runtime_spec(args))
        return
    spec = _spec_from_state(root, previous)
    stop(root)
    setup(spec, reset_data=False)


def run(spec: RuntimeSpec, command: list[str]) -> int:
    if not command:
        raise RuntimeError("The run command needs a command after '--'")
    state = setup(spec)
    result: subprocess.CompletedProcess[bytes] | None = None
    try:
        result = subprocess.run(  # noqa: S603 - caller explicitly supplies the CI or developer command.
            command,
            cwd=spec.root,
            env=_test_environment(spec, state),
            check=False,
        )
    finally:
        stop(spec.root, preserve_state=result is None or result.returncode != 0)
    return result.returncode


def _runtime_processes_are_live(state: dict[str, object]) -> bool:
    """Distinguish a live sandbox from diagnostic state retained after failed tests."""
    return all(
        isinstance(pid, int) and isinstance(marker, str) and _process_has_marker(pid, marker)
        for pid, marker in (
            (state.get("pynchy_pid"), state.get("pynchy_marker")),
            (state.get("temporal_pid"), state.get("temporal_marker")),
        )
    )


def execute(root: Path, command: list[str]) -> int:
    """Run a command against an already-running managed runtime."""
    if not command:
        raise RuntimeError("The exec command needs a command after '--'")
    state = _read_state(root)
    if state is None:
        raise RuntimeError("Runtime is not running; use setup or run first")
    if not _runtime_processes_are_live(state):
        raise RuntimeError(
            "Runtime is not live; its saved state is diagnostic only. "
            "Run stop, then setup or run first"
        )
    spec = _spec_from_state(root, state)
    result = subprocess.run(  # noqa: S603 - caller explicitly supplies the developer command.
        command,
        cwd=spec.root,
        env=_test_environment(spec, state),
        check=False,
    )
    return result.returncode


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="isolated Pynchy worktree or clean checkout (default: current directory)",
    )
    parser.add_argument("--namespace", help="validated Docker resource namespace")
    parser.add_argument("--server-port", type=int, help="Pynchy HTTP port")
    parser.add_argument("--gateway-port", type=int, help="LiteLLM host port")
    parser.add_argument("--temporal-port", type=int, help="Temporal development-server port")
    parser.add_argument(
        "command",
        choices=("setup", "up", "stop", "down", "restart", "status", "run", "exec"),
    )
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    return parser.parse_args()


def _run_command(args: argparse.Namespace, root: Path) -> int | None:
    if args.command in {"setup", "up"}:
        setup(_runtime_spec(args))
    elif args.command in {"stop", "down"}:
        stop(root)
    elif args.command == "restart":
        restart(root, args)
    elif args.command == "status":
        state = _read_state(root)
        if state is None:
            raise RuntimeError("Runtime is not running")
        sys.stdout.write(json.dumps(_redacted_state(state), indent=2, sort_keys=True) + "\n")
    elif args.command == "run":
        command = args.command_args
        if command[:1] == ["--"]:
            command = command[1:]
        return run(_runtime_spec(args), command)
    else:
        command = args.command_args
        if command[:1] == ["--"]:
            command = command[1:]
        return execute(root, command)
    return None


def main() -> None:
    args = _parse_args()
    root = args.root.expanduser().resolve()
    try:
        exit_code = _run_command(args, root)
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        raise SystemExit(1) from exc
    if exit_code is not None:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
