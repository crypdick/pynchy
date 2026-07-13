"""LiteLLM gateway — Docker container with PostgreSQL sidecar.

Runs a LiteLLM proxy as a Docker container.  All LLM routing config
(models, keys, budgets, load balancing) lives in the user-managed
``litellm_config.yaml`` — pynchy doesn't translate or duplicate it.

Pynchy generates an ephemeral master key at startup and passes it to
the container via ``LITELLM_MASTER_KEY``.  Agent containers authenticate
with this key, same as the builtin mode.

LiteLLM serves the native Anthropic Messages API at ``/v1/messages``
and OpenAI at ``/v1/chat/completions``, so agent containers work
without URL changes.

Env-var forwarding
~~~~~~~~~~~~~~~~~~

At startup the gateway scans ``litellm_config.yaml`` for all
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
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from pynchy.config import get_settings
from pynchy.host.container_manager.docker import (
    docker_available,
    ensure_image,
    ensure_network,
    remove_container,
    run_docker,
    stop_container,
    wait_healthy,
)
from pynchy.host.container_manager.litellm_config import (
    PLACEHOLDER_RE,
    LiteLLMConfigPreparer,
)
from pynchy.host.container_manager.runtime_names import (
    runtime_container_name,
    runtime_network_name,
)
from pynchy.logger import logger

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


# ---------------------------------------------------------------------------
# LiteLLMGateway
# ---------------------------------------------------------------------------


class LiteLLMGateway:
    """Gateway backed by a LiteLLM proxy Docker container.

    Pynchy generates an ephemeral master key and injects it into the
    container via ``LITELLM_MASTER_KEY``.  The litellm_config.yaml should
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

    def __init__(  # noqa: PLR0913, RUF100 - stable gateway constructor shared by orchestrator call sites.
        self,
        *,
        config_path: str,
        port: int,
        container_host: str,
        image: str,
        postgres_image: str,
        data_dir: Path,
        master_key: str,
        required_models: tuple[str, ...] = (),
    ) -> None:
        self.port = port
        self.container_host = container_host
        self.key: str = master_key
        self._required_models = tuple(dict.fromkeys(required_models))
        self._config_preparer = LiteLLMConfigPreparer(required_models=self._required_models)

        self._config_path = Path(config_path).resolve()
        self._image = image
        self._postgres_image = postgres_image
        self._data_dir = data_dir / "litellm"
        self._pg_data_dir = self._data_dir / "postgres"
        self._litellm_container = runtime_container_name("litellm")
        self._postgres_container = runtime_container_name("litellm-db")
        self._network_name = runtime_network_name("litellm-net")

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
    def _database_url(self) -> str:
        return (
            f"postgresql://{_POSTGRES_USER}:{self._pg_password}"
            f"@{self._postgres_container}:{_POSTGRES_PORT}/{_POSTGRES_DB}"
        )

    def has_provider(self, _name: str) -> bool:
        # LiteLLM handles provider resolution — always expose both URLs.
        # If a provider isn't configured, litellm returns a clear error.
        return True

    # ------------------------------------------------------------------
    # Env-var forwarding
    # ------------------------------------------------------------------

    # Vars that pynchy sets itself — never forward from host env.
    _GATEWAY_MANAGED_VARS = frozenset(
        {
            "LITELLM_MASTER_KEY",
            "LITELLM_SALT_KEY",
            "DATABASE_URL",
        }
    )

    @staticmethod
    def _resolve_env(config_path: Path) -> dict[str, str]:
        """Build a merged env dict from ``.env`` file + ``os.environ``.

        ``.env`` is expected as a sibling of the config file (= project
        root).  ``os.environ`` wins on conflicts.
        """
        from dotenv import (  # noqa: PLC0415, RUF100 - lazy import keeps optional dotenv dependency out of module startup.
            dotenv_values,
        )

        dotenv_path = config_path.parent / ".env"
        dotenv_vars = dotenv_values(dotenv_path) if dotenv_path.exists() else {}
        merged: dict[str, str] = {k: v for k, v in dotenv_vars.items() if v is not None}
        merged.update(os.environ)
        return merged

    @staticmethod
    def _collect_yaml_env_refs(config_path: Path, env: dict[str, str]) -> list[tuple[str, str]]:
        """Scan litellm config for ``os.environ/`` references and resolve from *env*.

        Returns ``(name, value)`` pairs for every referenced var that is
        set in *env*.  Gateway-managed vars are excluded.  Missing or
        placeholder vars produce a warning and are skipped.

        Callers should pass the **filtered** config path so that env vars
        belonging to filtered-out model entries are not forwarded.
        """
        text = config_path.read_text(encoding="utf-8")
        var_names = set(re.findall(r"os\.environ/(\w+)", text))
        var_names -= LiteLLMGateway._GATEWAY_MANAGED_VARS

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

    @staticmethod
    def _uses_chatgpt_provider(config_path: Path) -> bool:
        """Return True when the filtered config needs ChatGPT OAuth state."""
        return "chatgpt/" in config_path.read_text(encoding="utf-8")

    @staticmethod
    def _uses_phoenix_callback(config_path: Path) -> bool:
        """Return True when LiteLLM is configured to export traces to Phoenix."""
        import yaml  # noqa: PLC0415, RUF100 - lazy import keeps optional yaml dependency out of module startup.

        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            return False
        settings = config.get("litellm_settings") or {}
        if not isinstance(settings, dict):
            return False
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
        import aiohttp  # noqa: PLC0415, RUF100 - lazy import keeps optional aiohttp dependency out of module startup.

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
        except Exception as exc:  # noqa: BLE001, RUF100 - startup health check converts gateway failure into a required-host error.
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
            _OTEL_CONTENT_ENV: env.get(_OTEL_CONTENT_ENV, "SPAN_AND_EVENT"),
            _PHOENIX_ENDPOINT_ENV: endpoint,
            "PHOENIX_PROJECT_NAME": env.get("PHOENIX_PROJECT_NAME", "pynchy"),
        }
        if phoenix_api_key := env.get("PHOENIX_API_KEY"):
            resolved["PHOENIX_API_KEY"] = phoenix_api_key
        return resolved

    # Docker helpers are in _docker.py — imported at module level.

    # ------------------------------------------------------------------
    # PostgreSQL sidecar
    # ------------------------------------------------------------------

    async def _start_postgres(self) -> None:
        """Start the PostgreSQL sidecar and wait for it to accept connections."""
        self._pg_data_dir.mkdir(parents=True, exist_ok=True)
        await ensure_image(self._postgres_image)

        await remove_container(self._postgres_container)

        logger.info(
            "Starting PostgreSQL sidecar",
            image=self._postgres_image,
            data_dir=str(self._pg_data_dir),
        )

        await run_docker(
            "run", "-d",
            "--name", self._postgres_container,
            "--network", self._network_name,
            "-v", f"{self._pg_data_dir}:/var/lib/postgresql/data",
            "-e", f"POSTGRES_USER={_POSTGRES_USER}",
            "-e", f"POSTGRES_PASSWORD={self._pg_password}",
            "-e", f"POSTGRES_DB={_POSTGRES_DB}",
            "--restart", "unless-stopped",
            self._postgres_image,
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
                logger.error("PostgreSQL container exited", logs=logs.stdout[-2000:])
                msg = "PostgreSQL container failed to start — check logs above"
                raise RuntimeError(msg)

            await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        msg = f"PostgreSQL did not become ready within {_POSTGRES_HEALTH_TIMEOUT}s"
        raise TimeoutError(msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not docker_available():
            msg = "Docker is required for LiteLLM gateway mode but 'docker' was not found on PATH"
            raise RuntimeError(msg)

        if not self._config_path.exists():
            msg = f"LiteLLM config not found: {self._config_path}"
            raise FileNotFoundError(msg)

        self._data_dir.mkdir(parents=True, exist_ok=True)

        await ensure_network(self._network_name)
        await self._start_postgres()

        await ensure_image(self._image)

        # Remove any stale LiteLLM container before starting
        await remove_container(self._litellm_container)

        # Resolve env vars once — shared by config filtering and env-var forwarding.
        env = self._resolve_env(self._config_path)

        # Filter the config: remove model entries with missing/placeholder keys
        filtered_config = self._config_preparer.prepare(
            self._config_path,
            self._data_dir,
            env,
        )
        phoenix_env = self._phoenix_env_vars(filtered_config, env)
        if phoenix_env:
            await self._check_phoenix_ready(phoenix_env[_PHOENIX_ENDPOINT_ENV])

        logger.info(
            "Starting LiteLLM proxy container",
            image=self._image,
            config=str(filtered_config),
            port=self.port,
        )

        # Build environment variables
        env_vars = [
            "-e",
            f"LITELLM_MASTER_KEY={self.key}",
            "-e",
            f"LITELLM_SALT_KEY={self._salt_key}",
            "-e",
            f"DATABASE_URL={self._database_url}",
        ]
        if self._uses_chatgpt_provider(filtered_config):
            token_dir = env.get("CHATGPT_TOKEN_DIR") or "/app/data/chatgpt"
            env_vars.extend(["-e", f"CHATGPT_TOKEN_DIR={token_dir}"])
        for var_name, value in phoenix_env.items():
            env_vars.extend(["-e", f"{var_name}={value}"])

        # Forward env vars referenced in the *filtered* config so we don't
        # forward vars for model entries that were filtered out.
        for var_name, value in self._collect_yaml_env_refs(filtered_config, env):
            env_vars.extend(["-e", f"{var_name}={value}"])

        # Add UI credentials if configured
        s = get_settings()
        if s.gateway.ui_username:
            env_vars.extend(["-e", f"UI_USERNAME={s.gateway.ui_username}"])
        if s.gateway.ui_password:
            env_vars.extend(["-e", f"UI_PASSWORD={s.gateway.ui_password.get_secret_value()}"])

        await run_docker(
            "run", "-d",
            "--init",
            "--name", self._litellm_container,
            "--network", self._network_name,
            "-p", f"{self.port}:{_LITELLM_INTERNAL_PORT}",
            "-v", f"{filtered_config}:/app/config.yaml:ro",
            "-v", f"{self._data_dir}:/app/data",
            *env_vars,
            "--restart", "unless-stopped",
            self._image,
            "--config", "/app/config.yaml",
            "--port", str(_LITELLM_INTERNAL_PORT),
        )  # fmt: skip

        await wait_healthy(
            self._litellm_container,
            f"http://localhost:{self.port}/health/readiness",
            health_timeout_seconds=_HEALTH_TIMEOUT,
            poll_interval=_HEALTH_POLL_INTERVAL,
            headers={"Authorization": f"Bearer {self.key}"},
        )

        logger.info(
            "LiteLLM gateway ready",
            port=self.port,
            container_url=self.base_url,
            container=self._litellm_container,
        )

    async def stop(self) -> None:
        logger.info("Stopping LiteLLM gateway containers")
        await asyncio.gather(
            stop_container(self._litellm_container),
            stop_container(self._postgres_container),
        )
        await run_docker("network", "rm", self._network_name, check=False)
        logger.info("LiteLLM gateway stopped")
