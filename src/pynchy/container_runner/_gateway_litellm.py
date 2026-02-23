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

OAuth tokens
~~~~~~~~~~~~

Anthropic OAuth tokens (``sk-ant-oat01-…``) work as ``api_key`` values
in ``litellm_config.yaml``.  LiteLLM detects the ``sk-ant-oat*`` prefix
and automatically uses ``Authorization: Bearer`` with the required
``anthropic-beta: oauth-2025-04-20`` header (server-side, since PR #21039).
No ``extra_headers`` needed.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from pathlib import Path

from pynchy.config import get_settings
from pynchy.container_runner._docker import (
    docker_available,
    ensure_image,
    ensure_network,
    remove_container,
    run_docker,
    stop_container,
    wait_healthy,
)
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LITELLM_CONTAINER = "pynchy-litellm"
_POSTGRES_CONTAINER = "pynchy-litellm-db"
_NETWORK_NAME = "pynchy-litellm-net"
_LITELLM_INTERNAL_PORT = 4000
_POSTGRES_PORT = 5432
_POSTGRES_DB = "litellm"
_POSTGRES_USER = "litellm"
_HEALTH_TIMEOUT = 90  # seconds; Postgres + LiteLLM migrations need headroom
_HEALTH_POLL_INTERVAL = 1.0
_POSTGRES_HEALTH_TIMEOUT = 30

_SALT_KEY_FILE = "salt.key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_or_create_persistent_key(path: Path, prefix: str = "") -> str:
    """Read a key from disk, or generate and persist one on first run."""
    if path.exists():
        return path.read_text().strip()
    key = f"{prefix}{secrets.token_urlsafe(32)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
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

    def __init__(
        self,
        *,
        config_path: str,
        port: int,
        container_host: str,
        image: str,
        postgres_image: str,
        data_dir: Path,
        master_key: str,
    ) -> None:
        self.port = port
        self.container_host = container_host
        self.key: str = master_key

        self._config_path = Path(config_path).resolve()
        self._image = image
        self._postgres_image = postgres_image
        self._data_dir = data_dir / "litellm"
        self._pg_data_dir = self._data_dir / "postgres"

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
            f"@{_POSTGRES_CONTAINER}:{_POSTGRES_PORT}/{_POSTGRES_DB}"
        )

    def has_provider(self, name: str) -> bool:
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

    # Pattern matching obvious placeholder values in env vars.  Real API
    # keys never contain "..." or "YOUR_KEY", but placeholder .env lines
    # commonly do.  Forwarding a placeholder to LiteLLM creates a zombie
    # deployment that poisons the router's health state (auth errors
    # during startup probes mark all deployments as unhealthy).
    _PLACEHOLDER_RE = re.compile(r"\.\.\.|YOUR_|CHANGE_ME|REPLACE_|xxx{3,}", re.IGNORECASE)

    @staticmethod
    def _resolve_env(config_path: Path) -> dict[str, str]:
        """Build a merged env dict from ``.env`` file + ``os.environ``.

        ``.env`` is expected as a sibling of the config file (= project
        root).  ``os.environ`` wins on conflicts.
        """
        from dotenv import dotenv_values

        dotenv_path = config_path.parent / ".env"
        dotenv_vars = dotenv_values(dotenv_path) if dotenv_path.exists() else {}
        return {**dotenv_vars, **os.environ}

    @staticmethod
    def _collect_yaml_env_refs(config_path: Path) -> list[tuple[str, str]]:
        """Scan litellm config for ``os.environ/`` references and resolve from host env.

        Checks both ``os.environ`` and the project ``.env`` file (via
        python-dotenv).  ``os.environ`` wins on conflicts.

        Returns ``(name, value)`` pairs for every referenced var that is
        set on the host.  Gateway-managed vars are excluded.  Missing or
        placeholder vars produce a warning and are skipped.
        """
        text = config_path.read_text()
        var_names = set(re.findall(r"os\.environ/(\w+)", text))
        var_names -= LiteLLMGateway._GATEWAY_MANAGED_VARS

        env = LiteLLMGateway._resolve_env(config_path)

        resolved: list[tuple[str, str]] = []
        for name in sorted(var_names):
            value = env.get(name)
            if not value:
                logger.warning("YAML references unset env var", var=name)
            elif LiteLLMGateway._PLACEHOLDER_RE.search(value):
                logger.warning(
                    "Skipping env var with placeholder value",
                    var=name,
                )
            else:
                resolved.append((name, value))
        return resolved

    @staticmethod
    def _prepare_config(config_path: Path, output_dir: Path) -> Path:
        """Create a filtered copy of the litellm config.

        Removes ``model_list`` entries whose ``api_key`` references an
        env var that is unset or contains a placeholder value.  This
        prevents LiteLLM from loading zombie deployments that fail auth
        during startup health probes and poison the router's health state
        for *all* deployments.

        Returns the path to the filtered config (written inside
        *output_dir*).  If no entries are filtered, the file is still
        written — it's always the filtered copy that gets mounted.
        """
        import yaml

        env = LiteLLMGateway._resolve_env(config_path)
        config = yaml.safe_load(config_path.read_text())

        if not isinstance(config, dict) or "model_list" not in config:
            # Nothing to filter — write a verbatim copy
            out = output_dir / "litellm_config.yaml"
            out.write_text(config_path.read_text())
            return out

        original_count = len(config["model_list"])
        kept: list[dict] = []
        for entry in config["model_list"]:
            api_key = (entry.get("litellm_params") or {}).get("api_key", "")
            m = re.match(r"os\.environ/(\w+)", str(api_key))
            if m:
                var_name = m.group(1)
                value = env.get(var_name)
                if not value:
                    model_id = (entry.get("model_info") or {}).get("id", "?")
                    logger.warning(
                        "Removing model entry with unset api_key env var",
                        model_id=model_id,
                        var=var_name,
                    )
                    continue
                if LiteLLMGateway._PLACEHOLDER_RE.search(value):
                    model_id = (entry.get("model_info") or {}).get("id", "?")
                    logger.warning(
                        "Removing model entry with placeholder api_key",
                        model_id=model_id,
                        var=var_name,
                    )
                    continue
            kept.append(entry)

        config["model_list"] = kept

        removed = original_count - len(kept)
        if removed:
            logger.info(
                "Filtered litellm config",
                removed=removed,
                remaining=len(kept),
            )

        out = output_dir / "litellm_config.yaml"
        out.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
        return out

    # Docker helpers are in _docker.py — imported at module level.

    # ------------------------------------------------------------------
    # PostgreSQL sidecar
    # ------------------------------------------------------------------

    async def _start_postgres(self) -> None:
        """Start the PostgreSQL sidecar and wait for it to accept connections."""
        self._pg_data_dir.mkdir(parents=True, exist_ok=True)
        ensure_image(self._postgres_image)

        remove_container(_POSTGRES_CONTAINER)

        logger.info(
            "Starting PostgreSQL sidecar",
            image=self._postgres_image,
            data_dir=str(self._pg_data_dir),
        )

        run_docker(
            "run", "-d",
            "--name", _POSTGRES_CONTAINER,
            "--network", _NETWORK_NAME,
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
            result = run_docker(
                "exec",
                _POSTGRES_CONTAINER,
                "pg_isready",
                "-U",
                _POSTGRES_USER,
                check=False,
            )
            if result.returncode == 0:
                logger.info("PostgreSQL sidecar ready")
                return

            # Ensure the container is still running
            inspect = run_docker(
                "inspect",
                "-f",
                "{{.State.Running}}",
                _POSTGRES_CONTAINER,
                check=False,
            )
            if inspect.stdout.strip() != "true":
                logs = run_docker(
                    "logs",
                    "--tail",
                    "30",
                    _POSTGRES_CONTAINER,
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

        ensure_network(_NETWORK_NAME)
        await self._start_postgres()

        ensure_image(self._image)

        # Remove stale LiteLLM container from previous run
        remove_container(_LITELLM_CONTAINER)

        # Filter the config: remove model entries with missing/placeholder keys
        filtered_config = self._prepare_config(self._config_path, self._data_dir)

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

        # Forward env vars referenced in litellm_config.yaml
        for var_name, value in self._collect_yaml_env_refs(self._config_path):
            env_vars.extend(["-e", f"{var_name}={value}"])

        # Add UI credentials if configured
        s = get_settings()
        if s.gateway.ui_username:
            env_vars.extend(["-e", f"UI_USERNAME={s.gateway.ui_username}"])
        if s.gateway.ui_password:
            env_vars.extend(["-e", f"UI_PASSWORD={s.gateway.ui_password.get_secret_value()}"])

        run_docker(
            "run", "-d",
            "--init",
            "--name", _LITELLM_CONTAINER,
            "--network", _NETWORK_NAME,
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
            _LITELLM_CONTAINER,
            f"http://localhost:{self.port}/health",
            timeout=_HEALTH_TIMEOUT,
            poll_interval=_HEALTH_POLL_INTERVAL,
            headers={"Authorization": f"Bearer {self.key}"},
        )

        logger.info(
            "LiteLLM gateway ready",
            port=self.port,
            container_url=self.base_url,
            container=_LITELLM_CONTAINER,
        )

    async def stop(self) -> None:
        logger.info("Stopping LiteLLM gateway containers")
        stop_container(_LITELLM_CONTAINER)
        stop_container(_POSTGRES_CONTAINER)
        run_docker("network", "rm", _NETWORK_NAME, check=False)
        logger.info("LiteLLM gateway stopped")
