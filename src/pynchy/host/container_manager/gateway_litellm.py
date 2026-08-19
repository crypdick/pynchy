"""LiteLLM gateway — Docker container with PostgreSQL sidecar.

Runs a LiteLLM proxy as a Docker container.  All LLM routing config
(models, keys, budgets, load balancing) lives in the user-managed
``data/personalization/litellm.yaml`` — Pynchy filters it into a generated
runtime copy without modifying the source.

Pynchy generates an ephemeral master key at startup and passes it to
the container via ``LITELLM_MASTER_KEY``.  Agent containers authenticate
with this key, same as the builtin mode.

LiteLLM serves the native Anthropic Messages API at ``/v1/messages``
and OpenAI at ``/v1/chat/completions``, so agent containers work
without URL changes.

Env-var forwarding
~~~~~~~~~~~~~~~~~~

At startup the gateway scans personalized ``litellm.yaml`` for all
``os.environ/VARNAME`` references and forwards matching host env vars
into the Docker container via ``-e``.  The YAML is the single source of
truth — add model entries there, set the corresponding vars in ``.env``,
and pynchy picks them up automatically.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import aiohttp

from pynchy.host.container_manager import reaper
from pynchy.host.container_manager.docker import (
    HealthCheckRequest,
    docker_available,
    ensure_image,
    ensure_network,
    is_container_running,
    redacted_container_logs,
    remove_container,
    run_docker,
    stop_container,
    wait_healthy,
)
from pynchy.host.container_manager.litellm_config import (
    PLACEHOLDER_RE,
    LiteLLMConfigPreparer,
)
from pynchy.host.container_manager.litellm_responses import LiteLLMResponsesAvailability
from pynchy.logger import logger
from pynchy.redaction import (
    GatewayRedactionPosture,
    redaction_posture_for_gateway_mode,
)
from pynchy.runtime_names import (
    runtime_container_name,
    runtime_namespace,
    runtime_network_name,
    runtime_volume_name,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LITELLM_INTERNAL_PORT = 4000
_POSTGRES_PORT = 5432
_POSTGRES_DB = "litellm"
_POSTGRES_USER = "litellm"
_HEALTH_TIMEOUT = 90.0  # seconds; Postgres + LiteLLM migrations need headroom
_HEALTH_POLL_INTERVAL = 1.0
_POSTGRES_HEALTH_TIMEOUT = 30

_SALT_KEY_FILE = "salt.key"
_PHOENIX_CALLBACK = "arize_phoenix"
_PHOENIX_ENDPOINT_ENV = "PHOENIX_COLLECTOR_HTTP_ENDPOINT"
_OTEL_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
_GATEWAY_MANAGED_VARS = frozenset(
    {
        "LITELLM_MASTER_KEY",
        "LITELLM_SALT_KEY",
        "DATABASE_URL",
    }
)


@dataclass(frozen=True)
class LiteLLMGatewayCredentials:
    """Resolved optional credentials for LiteLLM's browser UI."""

    ui_username: str | None = None
    ui_password: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_or_create_persistent_key(path: Path, prefix: str = "") -> str:
    """Read a key from disk, or generate and persist one on first run."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = f"{prefix}{secrets.token_urlsafe(32)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    return key


def resolve_litellm_environment(config_path: Path) -> dict[str, str]:
    """Load the config's sibling ``.env`` and overlay the host environment."""
    from dotenv import (  # noqa: PLC0415 - lazy import keeps optional dotenv dependency out of module startup.
        dotenv_values,
    )

    dotenv_path = config_path.parent / ".env"
    dotenv_vars = dotenv_values(dotenv_path) if dotenv_path.exists() else {}
    merged: dict[str, str] = {key: value for key, value in dotenv_vars.items() if value is not None}
    merged.update(os.environ)
    return merged


def collect_litellm_yaml_environment(
    config_path: Path, env: dict[str, str]
) -> list[tuple[str, str]]:
    """Resolve non-gateway ``os.environ/`` references from a LiteLLM config."""
    text = config_path.read_text(encoding="utf-8")
    var_names = set(re.findall(r"os\.environ/(\w+)", text))
    var_names -= _GATEWAY_MANAGED_VARS

    resolved: list[tuple[str, str]] = []
    for name in sorted(var_names):
        value = env.get(name)
        if not value:
            logger.warning("YAML references unset env var", var=name)
        elif PLACEHOLDER_RE.search(value):
            logger.warning(
                "Skipping env var with placeholder value",
                var=name,
            )
        else:
            resolved.append((name, value))
    return resolved


# ---------------------------------------------------------------------------
# LiteLLMGateway
# ---------------------------------------------------------------------------


class LiteLLMGateway:
    """Gateway backed by a LiteLLM proxy Docker container.

    Pynchy generates an ephemeral master key and injects it into the
    container via ``LITELLM_MASTER_KEY``. The personalized LiteLLM YAML should
    reference it::

        general_settings:
          master_key: os.environ/LITELLM_MASTER_KEY

    Or omit ``master_key`` entirely — litellm reads the env var
    automatically.

    A PostgreSQL sidecar container provides persistent storage for
    spend tracking, provider budget caps, and virtual keys.  Both
    containers share a private Docker network.

    Attributes:
        port: Host port mapped to the litellm container.
        key: Ephemeral master key for container authentication.
    """

    def __init__(  # noqa: PLR0913 - stable gateway constructor shared by orchestrator call sites.
        self,
        *,
        config_path: str,
        port: int,
        container_host: str,
        image: str,
        postgres_image: str,
        data_dir: Path,
        master_key: str,
        managed: bool = True,
        required_models: tuple[str, ...] = (),
        required_response_models: tuple[str, ...] = (),
        ui_credentials: LiteLLMGatewayCredentials | None = None,
    ) -> None:
        self.port = port
        self.container_host = container_host
        self.key: str = master_key
        self.managed = managed
        self._required_models = tuple(dict.fromkeys(required_models))
        self._required_response_models = tuple(dict.fromkeys(required_response_models))
        self._ui_credentials = ui_credentials or LiteLLMGatewayCredentials()
        self._config_preparer = LiteLLMConfigPreparer(
            required_models=self._required_models,
            required_response_models=self._required_response_models,
        )
        self._responses = LiteLLMResponsesAvailability(port=port, key=master_key)

        self._config_path = Path(config_path).resolve()
        self._image = image
        self._postgres_image = postgres_image
        self._data_dir = data_dir / "litellm"
        self._pg_data_dir = self._data_dir / "postgres"
        self._litellm_container = runtime_container_name("litellm")
        self._postgres_container = runtime_container_name("litellm-db")
        self._network_name = runtime_network_name("litellm-net")
        self._postgres_volume = (
            None if runtime_namespace() == "pynchy" else runtime_volume_name("litellm-db-data")
        )

        self._pg_password = _load_or_create_persistent_key(
            self._data_dir / "pg_password.key",
        )
        self._salt_key = _load_or_create_persistent_key(
            self._data_dir / _SALT_KEY_FILE,
            prefix="sk-salt-",
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.container_host}:{self.port}"

    @property
    def redaction_posture(self) -> GatewayRedactionPosture:
        """Report that the external LiteLLM process bypasses Python enforcement."""
        return redaction_posture_for_gateway_mode("litellm")

    @property
    def required_models(self) -> tuple[str, ...]:
        """Return model aliases this gateway is configured to serve."""
        return self._required_models

    @property
    def required_response_models(self) -> tuple[str, ...]:
        """Return selected aliases that must support LiteLLM's Responses API."""
        return self._required_response_models

    @property
    def responses_status(self) -> dict[str, object]:
        """Return cached, sanitized Responses availability without blocking on a probe."""
        return self._responses.status

    @property
    def _database_url(self) -> str:
        return (
            f"postgresql://{_POSTGRES_USER}:{self._pg_password}"
            f"@{self._postgres_container}:{_POSTGRES_PORT}/{_POSTGRES_DB}"
        )

    @property
    def _postgres_mount_source(self) -> str:
        return self._postgres_volume or str(self._pg_data_dir)

    def has_provider(self, _name: str) -> bool:
        # LiteLLM handles provider resolution — always expose both URLs.
        # If a provider isn't configured, litellm returns a clear error.
        return True

    async def is_ready(self) -> bool:
        """Return whether both sidecars and the proxy readiness endpoint work."""
        if self.managed:
            litellm_running, postgres_running = await asyncio.gather(
                is_container_running(self._litellm_container),
                is_container_running(self._postgres_container),
            )
            if not litellm_running or not postgres_running:
                return False
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session,
                session.get(
                    f"http://localhost:{self.port}/health/readiness",
                    headers={"Authorization": f"Bearer {self.key}"},
                ) as response,
            ):
                return response.status == 200
        except (aiohttp.ClientError, OSError):
            return False

    # ------------------------------------------------------------------
    # Env-var forwarding
    # ------------------------------------------------------------------

    @staticmethod
    def _uses_chatgpt_provider(config_path: Path) -> bool:
        """Return True when the filtered config needs ChatGPT OAuth state."""
        return "chatgpt/" in config_path.read_text(encoding="utf-8")

    @staticmethod
    def _uses_phoenix_callback(config_path: Path) -> bool:
        """Return True when LiteLLM is configured to export traces to Phoenix."""
        import yaml  # noqa: PLC0415 - lazy import keeps optional yaml dependency out of module startup.

        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            return False
        settings = config.get("litellm_settings") or {}
        callbacks = settings.get("callbacks") or []
        if isinstance(callbacks, str):
            callbacks = [callbacks]
        return _PHOENIX_CALLBACK in callbacks

    @staticmethod
    def _phoenix_health_url(endpoint: str) -> str:
        """Map a Phoenix OTLP traces endpoint to the deployment health endpoint."""
        parsed = urlparse(endpoint)
        path = parsed.path.rstrip("/").removesuffix("/v1/traces")
        health_path = f"{path}/healthz" if path else "/healthz"
        return urlunparse(parsed._replace(path=health_path, params="", query="", fragment=""))

    async def _check_phoenix_ready(self, endpoint: str) -> None:
        """Fail startup when Phoenix is enabled but unreachable."""
        import aiohttp  # noqa: PLC0415 - lazy import keeps optional aiohttp dependency out of module startup.

        health_url = self._phoenix_health_url(endpoint)
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(health_url) as response,
            ):
                if response.status < 400:
                    return
                msg = f"Phoenix health check failed at {health_url}: HTTP {response.status}"
                raise RuntimeError(msg)
        except (
            Exception
        ) as exc:  # startup health check converts gateway failure into a required-host error.
            msg = f"Phoenix is required but not reachable at {health_url}: {exc}"
            raise RuntimeError(msg) from exc

    def _phoenix_env_vars(self, config_path: Path, env: dict[str, str]) -> dict[str, str]:
        """Resolve Phoenix/OTel env for LiteLLM when Phoenix callback is configured."""
        if not self._uses_phoenix_callback(config_path):
            return {}

        endpoint = env.get(_PHOENIX_ENDPOINT_ENV, "").strip()
        if not endpoint:
            msg = (
                "LiteLLM Phoenix tracing is enabled but "
                f"{_PHOENIX_ENDPOINT_ENV} is not set in .env or the host environment"
            )
            raise RuntimeError(msg)

        resolved = {
            "LITELLM_OTEL_V2": env.get("LITELLM_OTEL_V2", "true"),
            _OTEL_CONTENT_ENV: "NO_CONTENT",
            _PHOENIX_ENDPOINT_ENV: endpoint,
            "PHOENIX_PROJECT_NAME": env.get("PHOENIX_PROJECT_NAME", "pynchy"),
        }
        if phoenix_api_key := env.get("PHOENIX_API_KEY"):
            resolved["PHOENIX_API_KEY"] = phoenix_api_key
        return resolved

    # Docker helpers are in docker.py — imported at module level.

    # ------------------------------------------------------------------
    # PostgreSQL sidecar
    # ------------------------------------------------------------------

    async def _start_postgres(self) -> None:
        """Start the PostgreSQL sidecar and wait for it to accept connections."""
        if self._postgres_volume is None:
            self._pg_data_dir.mkdir(parents=True, exist_ok=True)
        await ensure_image(self._postgres_image)

        await remove_container(self._postgres_container)

        logger.info(
            "Starting PostgreSQL sidecar",
            image=self._postgres_image,
            storage=self._postgres_mount_source,
        )

        postgres_environment = {
            "POSTGRES_USER": _POSTGRES_USER,
            "POSTGRES_PASSWORD": self._pg_password,
            "POSTGRES_DB": _POSTGRES_DB,
        }
        await run_docker(
            "run", "-d",
            "--name", self._postgres_container,
            "--network", self._network_name,
            "-v", f"{self._postgres_mount_source}:/var/lib/postgresql/data",
            "-e", "POSTGRES_USER",
            "-e", "POSTGRES_PASSWORD",
            "-e", "POSTGRES_DB",
            *reaper.runtime_restart_policy_args(),
            *reaper.runtime_provenance_label_args(),
            self._postgres_image,
            environment=postgres_environment,
        )  # fmt: skip

        await self._wait_postgres_healthy()

    async def _wait_postgres_healthy(self) -> None:
        """Poll pg_isready inside the container until Postgres is up."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _POSTGRES_HEALTH_TIMEOUT

        while loop.time() < deadline:
            result = await run_docker(
                "exec",
                self._postgres_container,
                "pg_isready",
                "-U",
                _POSTGRES_USER,
                check=False,
            )
            if result.returncode == 0:
                logger.info("PostgreSQL sidecar ready")
                return

            # Ensure the container is still running
            inspect = await run_docker(
                "inspect",
                "-f",
                "{{.State.Running}}",
                self._postgres_container,
                check=False,
            )
            if inspect.stdout.strip() != "true":
                logs = await run_docker(
                    "logs",
                    "--tail",
                    "30",
                    self._postgres_container,
                    check=False,
                )
                logger.error(
                    "PostgreSQL container exited",
                    logs=redacted_container_logs(logs, limit=2000),
                )
                msg = "PostgreSQL container failed to start — check logs above"
                raise RuntimeError(msg)

            await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        msg = f"PostgreSQL did not become ready within {_POSTGRES_HEALTH_TIMEOUT}s"
        raise TimeoutError(msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self.managed and not docker_available():
            msg = "Docker is required for LiteLLM gateway mode but 'docker' was not found on PATH"
            raise RuntimeError(msg)

        if not self._config_path.exists():
            msg = f"LiteLLM config not found: {self._config_path}"
            raise FileNotFoundError(msg)

        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Validate and resolve the complete launch configuration before
        # mutating container-runtime state. A missing required dependency must
        # not leave a network, database, or partial gateway container behind.
        env = resolve_litellm_environment(self._config_path)
        prepared_config = self._config_preparer.prepare(
            self._config_path,
            self._data_dir,
            env,
        )
        filtered_config = prepared_config.path
        self._responses.set_routes(prepared_config.response_routes)
        phoenix_env = self._phoenix_env_vars(filtered_config, env)
        if phoenix_env:
            await self._check_phoenix_ready(phoenix_env[_PHOENIX_ENDPOINT_ENV])

        if not self.managed:
            await wait_healthy(
                HealthCheckRequest(
                    container_name=None,
                    url=f"http://localhost:{self.port}/health/readiness",
                    health_timeout_seconds=_HEALTH_TIMEOUT,
                    poll_interval=_HEALTH_POLL_INTERVAL,
                    headers={"Authorization": f"Bearer {self.key}"},
                )
            )
            await self._responses.refresh()
            logger.info("External LiteLLM ready", port=self.port, container_url=self.base_url)
            return

        container_environment = {
            "LITELLM_MASTER_KEY": self.key,
            "LITELLM_SALT_KEY": self._salt_key,
            "DATABASE_URL": self._database_url,
        }
        if self._uses_chatgpt_provider(filtered_config):
            container_environment["CHATGPT_TOKEN_DIR"] = (
                env.get("CHATGPT_TOKEN_DIR") or "/app/data/chatgpt"
            )
        container_environment.update(phoenix_env)

        # Forward env vars referenced in the *filtered* config so we don't
        # forward vars for model entries that were filtered out.
        container_environment.update(collect_litellm_yaml_environment(filtered_config, env))

        if self._ui_credentials.ui_username:
            container_environment["UI_USERNAME"] = self._ui_credentials.ui_username
        if self._ui_credentials.ui_password:
            container_environment["UI_PASSWORD"] = self._ui_credentials.ui_password
        env_args = [argument for name in sorted(container_environment) for argument in ("-e", name)]

        await ensure_network(self._network_name)
        await self._start_postgres()
        await ensure_image(self._image)
        await remove_container(self._litellm_container)

        logger.info(
            "Starting LiteLLM proxy container",
            image=self._image,
            config=str(filtered_config),
            port=self.port,
        )

        await run_docker(
            "run", "-d",
            "--init",
            "--name", self._litellm_container,
            "--network", self._network_name,
            "-p", f"{self.port}:{_LITELLM_INTERNAL_PORT}",
            "-v", f"{filtered_config}:/app/config.yaml:ro",
            "-v", f"{self._data_dir}:/app/data",
            *env_args,
            *reaper.runtime_restart_policy_args(),
            *reaper.runtime_provenance_label_args(),
            self._image,
            "--config", "/app/config.yaml",
            "--port", str(_LITELLM_INTERNAL_PORT),
            environment=container_environment,
        )  # fmt: skip

        await wait_healthy(
            HealthCheckRequest(
                container_name=self._litellm_container,
                url=f"http://localhost:{self.port}/health/readiness",
                health_timeout_seconds=_HEALTH_TIMEOUT,
                poll_interval=_HEALTH_POLL_INTERVAL,
                headers={"Authorization": f"Bearer {self.key}"},
            )
        )
        # HTTP and IPC status surfaces publish only after gateway startup finishes,
        # so this initial snapshot cannot race a status-triggered refresh.
        await self._responses.refresh()

        logger.info(
            "LiteLLM proxy and database ready",
            port=self.port,
            container_url=self.base_url,
            container=self._litellm_container,
            responses_state=self._responses.state,
        )

    async def stop(self) -> None:
        await self._responses.stop()
        if not self.managed:
            return
        logger.info("Stopping LiteLLM gateway containers")
        await asyncio.gather(
            stop_container(self._litellm_container),
            stop_container(self._postgres_container),
        )
        await run_docker("network", "rm", self._network_name, check=False)
        logger.info("LiteLLM gateway stopped")
