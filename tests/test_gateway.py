"""Tests for the LLM gateway — LiteLLM and Builtin modes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from pynchy.host.container_manager.docker import HealthCheckRequest
from pynchy.host.container_manager.gateway import (
    BuiltinGateway,
    LiteLLMGateway,
)
from pynchy.host.container_manager.gateway_builtin import (
    BuiltinGatewayCredentials,
    build_upstream_headers,
)
from pynchy.host.container_manager.gateway_litellm import (
    LiteLLMGatewayCredentials,
    collect_litellm_yaml_environment,
    resolve_litellm_environment,
)
from pynchy.host.container_manager.litellm_config import LiteLLMConfigPreparer
from pynchy.redaction import GatewayRedactionPosture

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# LiteLLMGateway — unit tests (Docker calls mocked)
# ---------------------------------------------------------------------------

_GATEWAY_MOD = "pynchy.host.container_manager.gateway"
_LITELLM_MOD = "pynchy.host.container_manager.gateway_litellm"
_DOCKER_MOD = "pynchy.host.container_manager.docker"
ALL_INTERFACE_BIND_HOST = "0.0.0.0"  # noqa: S104 - test data for intentional container-reachable gateway binds.

_LITELLM_KWARGS = {
    "port": 4000,
    "container_host": "host.docker.internal",
    "image": "ghcr.io/berriai/litellm:main-latest",
    "postgres_image": "postgres:17-alpine",
    "master_key": "test-master-key",
}


class TestLiteLLMGatewayInit:
    def test_uses_configured_master_key(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        assert gw.key == "test-master-key"

    def test_base_url(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        assert gw.base_url == "http://host.docker.internal:4000"

    def test_has_provider_always_true(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        assert gw.has_provider("anthropic") is True
        assert gw.has_provider("openai") is True
        assert gw.has_provider("anything") is True

    def test_reports_truthful_redaction_posture(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )

        assert gw.redaction_posture is GatewayRedactionPosture.NOT_ENFORCED

    def test_persists_salt_and_pg_password(self, tmp_path: Path):
        LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        keys_dir = tmp_path / "litellm"
        initial_salt = (keys_dir / "salt.key").read_text()
        initial_password = (keys_dir / "pg_password.key").read_text()

        LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        assert (keys_dir / "salt.key").read_text() == initial_salt
        assert (keys_dir / "pg_password.key").read_text() == initial_password


class TestCollectYamlEnvRefs:
    """Verify LiteLLM config environment resolution."""

    def test_finds_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            "  - litellm_params:\n"
            "      api_key: os.environ/FOO_TOKEN\n"
            "  - litellm_params:\n"
            "      api_key: os.environ/BAR_TOKEN\n"
        )
        monkeypatch.setenv("FOO_TOKEN", "foo-val")
        monkeypatch.setenv("BAR_TOKEN", "bar-val")

        env = resolve_litellm_environment(cfg)
        result = collect_litellm_yaml_environment(cfg, env)
        assert ("BAR_TOKEN", "bar-val") in result
        assert ("FOO_TOKEN", "foo-val") in result
        assert len(result) == 2

    def test_skips_gateway_managed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "general_settings:\n"
            "  master_key: os.environ/LITELLM_MASTER_KEY\n"
            "model_list:\n"
            "  - litellm_params:\n"
            "      api_key: os.environ/MY_KEY\n"
        )
        monkeypatch.setenv("LITELLM_MASTER_KEY", "should-not-appear")
        monkeypatch.setenv("MY_KEY", "my-val")

        env = resolve_litellm_environment(cfg)
        result = collect_litellm_yaml_environment(cfg, env)
        names = [name for name, _ in result]
        assert "LITELLM_MASTER_KEY" not in names
        assert "MY_KEY" in names

    def test_warns_on_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("api_key: os.environ/MISSING_VAR\n")
        monkeypatch.delenv("MISSING_VAR", raising=False)

        env = resolve_litellm_environment(cfg)
        result = collect_litellm_yaml_environment(cfg, env)
        assert result == []

    def test_reads_from_dotenv_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("api_key: os.environ/DOTENV_ONLY_TOKEN\n")
        monkeypatch.delenv("DOTENV_ONLY_TOKEN", raising=False)

        # .env as sibling of config file — no CWD dependency
        (tmp_path / ".env").write_text("DOTENV_ONLY_TOKEN=from-dotenv\n")

        env = resolve_litellm_environment(cfg)
        result = collect_litellm_yaml_environment(cfg, env)
        assert ("DOTENV_ONLY_TOKEN", "from-dotenv") in result

    @pytest.mark.asyncio
    async def test_start_forwards_yaml_env_vars(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Verify start() forwards env vars from YAML into docker run."""
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("api_key: os.environ/OPENAI_API_KEY_TEST\n")
        monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test")

        gw = LiteLLMGateway(config_path=str(cfg), data_dir=tmp_path, **_LITELLM_KWARGS)

        calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_docker(
            *args: str,
            environment: dict[str, str] | None = None,
            **_kwargs,
        ):
            calls.append((list(args), dict(environment or {})))
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            return result

        with (
            patch("pynchy.host.container_manager.docker.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_DOCKER_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch.object(gw, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.wait_healthy", new_callable=AsyncMock),
        ):
            await gw.start()

        litellm_args, litellm_environment = next(
            call for call in calls if "LITELLM_MASTER_KEY" in call[0]
        )
        assert "OPENAI_API_KEY_TEST" in litellm_args
        assert "sk-test" not in litellm_args
        assert litellm_environment["OPENAI_API_KEY_TEST"] == "sk-test"  # pragma: allowlist secret

    @pytest.mark.asyncio
    async def test_start_pins_chatgpt_token_dir(self, tmp_path: Path):
        """ChatGPT subscription OAuth tokens should persist under /app/data."""
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            "  - model_name: gpt-5.5\n"
            "    litellm_params:\n"
            "      model: chatgpt/gpt-5.5\n"
        )

        gw = LiteLLMGateway(config_path=str(cfg), data_dir=tmp_path, **_LITELLM_KWARGS)
        calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_docker(
            *args: str,
            environment: dict[str, str] | None = None,
            **_kwargs,
        ):
            calls.append((list(args), dict(environment or {})))
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            return result

        with (
            patch("pynchy.host.container_manager.docker.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_DOCKER_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch.object(gw, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.wait_healthy", new_callable=AsyncMock),
        ):
            await gw.start()

        litellm_args, litellm_environment = next(
            call for call in calls if "LITELLM_MASTER_KEY" in call[0]
        )
        assert "CHATGPT_TOKEN_DIR" in litellm_args
        assert "/app/data/chatgpt" not in litellm_args
        assert litellm_environment["CHATGPT_TOKEN_DIR"] == "/app/data/chatgpt"  # noqa: S105

    @pytest.mark.asyncio
    async def test_start_requires_phoenix_endpoint_when_callback_enabled(self, tmp_path: Path):
        """Phoenix tracing is a source-of-truth dependency, not a best-effort sink."""
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text('litellm_settings:\n  callbacks: ["arize_phoenix"]\n')

        gw = LiteLLMGateway(config_path=str(cfg), data_dir=tmp_path, **_LITELLM_KWARGS)

        with (
            patch("pynchy.host.container_manager.docker.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock) as ensure_network,
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock) as ensure_image,
            patch(f"{_LITELLM_MOD}.remove_container", new_callable=AsyncMock) as remove_container,
            patch(f"{_LITELLM_MOD}.run_docker", new_callable=AsyncMock) as run_docker,
            patch.object(gw, "_start_postgres", new_callable=AsyncMock) as start_postgres,
            pytest.raises(RuntimeError, match="PHOENIX_COLLECTOR_HTTP_ENDPOINT"),
        ):
            await gw.start()

        ensure_network.assert_not_awaited()
        start_postgres.assert_not_awaited()
        ensure_image.assert_not_awaited()
        remove_container.assert_not_awaited()
        run_docker.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_forwards_phoenix_env_without_content_capture(self, tmp_path: Path):
        """Phoenix receives metadata without prompt or response content."""
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text('litellm_settings:\n  callbacks: ["arize_phoenix"]\n')
        (tmp_path / ".env").write_text(
            "PHOENIX_COLLECTOR_HTTP_ENDPOINT=https://phoenix.example.test/v1/traces\n"
            "PHOENIX_PROJECT_NAME=pynchy-test\n"  # pragma: allowlist secret
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT\n"
        )

        gw = LiteLLMGateway(config_path=str(cfg), data_dir=tmp_path, **_LITELLM_KWARGS)
        calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_docker(
            *args: str,
            environment: dict[str, str] | None = None,
            **_kwargs,
        ):
            calls.append((list(args), dict(environment or {})))
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            return result

        with (
            patch("pynchy.host.container_manager.docker.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_DOCKER_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch.object(gw, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch.object(gw, "_check_phoenix_ready", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.wait_healthy", new_callable=AsyncMock),
        ):
            await gw.start()

        litellm_args, litellm_environment = next(
            call for call in calls if "LITELLM_MASTER_KEY" in call[0]
        )
        assert "https://phoenix.example.test/v1/traces" not in litellm_args
        assert litellm_environment["PHOENIX_COLLECTOR_HTTP_ENDPOINT"] == (
            "https://phoenix.example.test/v1/traces"
        )
        assert litellm_environment["PHOENIX_PROJECT_NAME"] == "pynchy-test"
        assert litellm_environment["LITELLM_OTEL_V2"] == "true"
        assert (
            litellm_environment["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"]
            == "NO_CONTENT"
        )


class TestPrepareLiteLLMConfig:
    def test_forces_metadata_only_logging(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        cfg.write_text(
            "model_list:\n"
            "  - model_name: test\n"
            "    litellm_params:\n"
            "      model: openai/test\n"
            "litellm_settings:\n"
            "  turn_off_message_logging: false\n"
            "  log_raw_request_response: true\n"
        )

        prepared = LiteLLMConfigPreparer().prepare(cfg, runtime_dir, env={})
        settings = yaml.safe_load(prepared.path.read_text())["litellm_settings"]

        assert settings["turn_off_message_logging"] is True
        assert settings["log_raw_request_response"] is False

    def test_raises_typeerror_when_model_list_is_not_a_list(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: not-a-list\n")

        with pytest.raises(TypeError, match="model_list must be a list"):
            LiteLLMConfigPreparer().prepare(cfg, tmp_path, env={})

    def test_raises_typeerror_when_model_list_entries_are_not_mappings(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list:\n  - model_name: gpt-5.5\n  - not-a-mapping\n")

        with pytest.raises(TypeError, match="model_list entries must be mappings"):
            LiteLLMConfigPreparer().prepare(cfg, tmp_path, env={})

    def test_raises_when_all_model_routes_are_filtered(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            "  - model_name: gpt-5.5\n"
            "    model_info:\n"
            "      id: openai-as-gpt-5.5\n"
            "    litellm_params:\n"
            "      model: openai/gpt-5.5\n"
            "      api_key: os.environ/OPENAI_KEY_AS\n"
        )

        with pytest.raises(RuntimeError, match="No usable LiteLLM model routes remain"):
            LiteLLMConfigPreparer().prepare(cfg, tmp_path, env={})

    def test_raises_when_required_model_route_is_missing(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            "  - model_name: other-model\n"
            "    litellm_params:\n"
            "      model: chatgpt/other-model\n"
        )

        with pytest.raises(RuntimeError, match=r"gpt-5\.5"):
            LiteLLMConfigPreparer(required_models=("gpt-5.5",)).prepare(cfg, tmp_path, env={})

    def test_required_model_can_match_provider_wildcard_route(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n  - model_name: openai/*\n    litellm_params:\n      model: openai/*\n"
        )

        LiteLLMConfigPreparer(required_models=("openai/gpt-5.5",)).prepare(
            cfg,
            tmp_path,
            env={},
        )


class TestLiteLLMGatewayStart:
    @staticmethod
    def _fake_docker_recorder(
        calls: list[list[str]],
        environments: list[dict[str, str]] | None = None,
    ):
        def fake_docker(
            *args: str,
            environment: dict[str, str] | None = None,
            **_kwargs,
        ):
            calls.append(list(args))
            if environments is not None:
                environments.append(dict(environment or {}))
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            return result

        return fake_docker

    @staticmethod
    def _joined_calls(calls: list[list[str]]) -> list[str]:
        return [" ".join(command) for command in calls]

    @staticmethod
    def _litellm_run_command(flat_calls: list[str]) -> str:
        return next(command for command in flat_calls if "LITELLM_MASTER_KEY" in command)

    @classmethod
    def _assert_start_calls(
        cls,
        calls: list[list[str]],
        wait_healthy_mock: AsyncMock,
    ) -> None:
        flat_calls = cls._joined_calls(calls)
        assert any("pynchy-litellm-db" in command and "run" in command for command in flat_calls)
        assert any(
            "pynchy-litellm" in command and "run" in command and "LITELLM_MASTER_KEY" in command
            for command in flat_calls
        )

        litellm_run = cls._litellm_run_command(flat_calls)
        assert "DATABASE_URL" in litellm_run
        assert "postgresql://" not in litellm_run
        assert "LITELLM_SALT_KEY" in litellm_run
        assert "--network pynchy-litellm-net" in litellm_run
        request = wait_healthy_mock.await_args.args[0]
        assert isinstance(request, HealthCheckRequest)
        assert request.url == "http://localhost:4000/health/readiness"
        assert request.health_timeout_seconds == pytest.approx(180.0)

    @pytest.fixture
    def litellm_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            "  - model_name: gpt-5.5\n"
            "    litellm_params:\n"
            "      model: chatgpt/gpt-5.5\n"
        )
        return cfg

    @pytest.fixture
    def gw(self, litellm_config: Path, tmp_path: Path) -> LiteLLMGateway:
        return LiteLLMGateway(
            config_path=str(litellm_config),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )

    @pytest.mark.asyncio
    async def test_raises_if_docker_not_found(self, gw: LiteLLMGateway):
        with (
            patch(f"{_LITELLM_MOD}.docker_available", return_value=False),
            pytest.raises(RuntimeError, match="Docker is required"),
        ):
            await gw.start()

    @pytest.mark.asyncio
    async def test_raises_if_config_missing(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "nonexistent.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        with (
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            pytest.raises(FileNotFoundError, match="LiteLLM config not found"),
        ):
            await gw.start()

    @pytest.mark.asyncio
    async def test_start_creates_network_and_postgres(self, litellm_config: Path, tmp_path: Path):
        """Verify start creates network, Postgres, then LiteLLM."""
        gw = LiteLLMGateway(
            config_path=str(litellm_config),
            data_dir=tmp_path,
            ui_credentials=LiteLLMGatewayCredentials(
                ui_username="dashboard-user",
                ui_password="dashboard-password",  # noqa: S106  # pragma: allowlist secret
            ),
            **_LITELLM_KWARGS,
        )
        calls: list[list[str]] = []
        environments: list[dict[str, str]] = []

        with (
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(
                f"{_LITELLM_MOD}.run_docker",
                new_callable=AsyncMock,
                side_effect=self._fake_docker_recorder(calls, environments),
            ),
            patch(
                f"{_DOCKER_MOD}.run_docker",
                new_callable=AsyncMock,
                side_effect=self._fake_docker_recorder(calls, environments),
            ),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch.object(gw, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.wait_healthy", new_callable=AsyncMock) as wait_healthy_mock,
        ):
            await gw.start()

        self._assert_start_calls(calls, wait_healthy_mock)
        litellm_run = self._litellm_run_command(self._joined_calls(calls))
        litellm_environment = next(
            environment for environment in environments if "LITELLM_MASTER_KEY" in environment
        )
        assert "dashboard-user" not in litellm_run
        assert "dashboard-password" not in litellm_run
        assert litellm_environment["UI_USERNAME"] == "dashboard-user"
        assert litellm_environment["UI_PASSWORD"] == "dashboard-password"  # noqa: S105  # pragma: allowlist secret

    @pytest.mark.asyncio
    async def test_start_accepts_non_mapping_phoenix_config(self, tmp_path: Path):
        """Malformed optional Phoenix settings do not block gateway startup."""
        config = tmp_path / "litellm_config.yaml"
        config.write_text("- malformed\n")
        gw = LiteLLMGateway(config_path=str(config), data_dir=tmp_path, **_LITELLM_KWARGS)
        calls: list[list[str]] = []

        with (
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(
                f"{_LITELLM_MOD}.run_docker",
                new_callable=AsyncMock,
                side_effect=self._fake_docker_recorder(calls),
            ),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch.object(gw, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.wait_healthy", new_callable=AsyncMock),
        ):
            await gw.start()

        assert any("pynchy-litellm" in " ".join(command) for command in calls)

    @pytest.mark.asyncio
    async def test_start_times_out_when_postgres_never_becomes_ready(self, tmp_path: Path):
        """Gateway startup reports a PostgreSQL sidecar that never becomes ready."""
        config = tmp_path / "litellm_config.yaml"
        config.write_text("litellm_settings: {}\n")
        gw = LiteLLMGateway(config_path=str(config), data_dir=tmp_path, **_LITELLM_KWARGS)
        calls: list[list[str]] = []

        class _Clock:
            def __init__(self) -> None:
                self._times = iter((0.0, 0.0, 30.0))

            def time(self) -> float:
                return next(self._times)

        def fake_docker(*args: str, **_kwargs: object) -> MagicMock:
            calls.append(list(args))
            result = MagicMock()
            result.returncode = 1 if args[0] == "exec" else 0
            result.stdout = "true" if args[0] == "inspect" else ""
            return result

        with (
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.remove_container", new_callable=AsyncMock),
            patch(
                f"{_LITELLM_MOD}.run_docker",
                new_callable=AsyncMock,
                side_effect=fake_docker,
            ),
            patch(f"{_LITELLM_MOD}.asyncio.get_running_loop", return_value=_Clock()),
            patch(f"{_LITELLM_MOD}.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(TimeoutError, match="PostgreSQL did not become ready"),
        ):
            await gw.start()

        assert ["exec", "pynchy-litellm-db", "pg_isready", "-U", "litellm"] in calls
        assert not any(_LITELLM_KWARGS["image"] in command for command in calls)

    @pytest.mark.asyncio
    async def test_stop_removes_all_containers_and_network(self, gw: LiteLLMGateway):
        calls: list[list[str]] = []

        def fake_docker(*args: str, **_kwargs):
            calls.append(list(args))
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            return result

        with (
            patch(f"{_LITELLM_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_DOCKER_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
        ):
            await gw.stop()

        assert ["stop", "-t", "5", "pynchy-litellm"] in calls
        assert ["rm", "-f", "pynchy-litellm"] in calls
        assert ["stop", "-t", "5", "pynchy-litellm-db"] in calls
        assert ["rm", "-f", "pynchy-litellm-db"] in calls
        assert ["network", "rm", "pynchy-litellm-net"] in calls


class TestBuiltinGateway:
    def test_generates_ephemeral_key(self):
        gw = BuiltinGateway(
            port=4010, host=ALL_INTERFACE_BIND_HOST, container_host="host.docker.internal"
        )
        assert gw.key.startswith("gw-")
        assert len(gw.key) > 20

    def test_base_url(self):
        gw = BuiltinGateway(
            port=4010, host=ALL_INTERFACE_BIND_HOST, container_host="host.docker.internal"
        )
        assert gw.base_url == "http://host.docker.internal:4010"

    def test_has_provider_false_before_start(self):
        gw = BuiltinGateway(
            port=4010, host=ALL_INTERFACE_BIND_HOST, container_host="host.docker.internal"
        )
        assert gw.has_provider("anthropic") is False
        assert gw.has_provider("openai") is False

    @pytest.mark.asyncio
    async def test_stop_before_start_is_a_noop(self):
        gw = BuiltinGateway(
            port=4010, host=ALL_INTERFACE_BIND_HOST, container_host="host.docker.internal"
        )

        await gw.stop()

    @pytest.mark.asyncio
    async def test_uses_injected_credentials_after_start(self):
        gw = BuiltinGateway(
            port=0,
            host=ALL_INTERFACE_BIND_HOST,
            container_host="host.docker.internal",
            credentials=BuiltinGatewayCredentials(
                anthropic_api_key="anthropic-credential",  # pragma: allowlist secret
                openai_api_key="openai-credential",  # pragma: allowlist secret
            ),
        )

        await gw.start()
        try:
            assert gw.has_provider("anthropic") is True
            assert gw.has_provider("openai") is True
        finally:
            await gw.stop()

    def test_redacts_complete_request_at_owned_gateway_boundary(self):
        gw = BuiltinGateway(
            port=4010,
            host=ALL_INTERFACE_BIND_HOST,
            container_host="host.docker.internal",
        )
        secret = "".join(("sk-", "a" * 32))
        body = json.dumps(
            {
                "instructions": "Contact person@example.test",
                "input": f"Use {secret}",
            }
        ).encode()

        forwarded = gw.prepare_upstream_body(body)

        assert gw.redaction_posture is GatewayRedactionPosture.ENFORCED
        assert secret.encode() not in forwarded
        assert b"person@example.test" not in forwarded


class TestBuiltinGatewayAuthHeaders:
    """Builtin gateway uses provider-native API-key headers."""

    def test_anthropic_creds_use_x_api_key(self):
        headers = build_upstream_headers({}, "anthropic", "sk-secret")
        assert headers["x-api-key"] == "sk-secret"
        assert "Authorization" not in headers
        assert "anthropic-beta" not in headers

    def test_openai_creds_use_bearer_auth(self):
        headers = build_upstream_headers({}, "openai", "sk-secret")
        assert headers["Authorization"] == "Bearer sk-secret"
        assert "x-api-key" not in headers

    def test_unknown_provider_does_not_add_provider_auth(self):
        assert build_upstream_headers({}, "unknown", "sk-secret") == {}
