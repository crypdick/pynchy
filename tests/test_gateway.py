"""Tests for the LLM gateway — LiteLLM and Builtin modes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings
from pydantic import SecretStr

import pynchy.host.container_manager.gateway as _gw_mod
from pynchy.host.container_manager.gateway import (
    BuiltinGateway,
    LiteLLMGateway,
    start_gateway,
)
from pynchy.host.container_manager.litellm_config import LiteLLMConfigPreparer

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# LiteLLMGateway — unit tests (Docker calls mocked)
# ---------------------------------------------------------------------------

_GATEWAY_MOD = "pynchy.host.container_manager.gateway"
_LITELLM_MOD = "pynchy.host.container_manager.gateway_litellm"
_DOCKER_MOD = "pynchy.host.container_manager.docker"
ALL_INTERFACE_BIND_HOST = "0.0.0.0"  # noqa: S104, RUF100 - test data for intentional container-reachable gateway binds.

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

    def test_database_url_format(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        assert gw._database_url.startswith("postgresql://litellm:")
        assert "@pynchy-litellm-db:5432/litellm" in gw._database_url

    def test_persists_salt_and_pg_password(self, tmp_path: Path):
        gw1 = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        gw2 = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        assert gw1._salt_key == gw2._salt_key
        assert gw1._pg_password == gw2._pg_password


class TestCollectYamlEnvRefs:
    """Verify _collect_yaml_env_refs scans YAML and resolves from host env."""

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

        env = LiteLLMGateway._resolve_env(cfg)
        result = LiteLLMGateway._collect_yaml_env_refs(cfg, env)
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

        env = LiteLLMGateway._resolve_env(cfg)
        result = LiteLLMGateway._collect_yaml_env_refs(cfg, env)
        names = [name for name, _ in result]
        assert "LITELLM_MASTER_KEY" not in names
        assert "MY_KEY" in names

    def test_warns_on_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("api_key: os.environ/MISSING_VAR\n")
        monkeypatch.delenv("MISSING_VAR", raising=False)

        env = LiteLLMGateway._resolve_env(cfg)
        result = LiteLLMGateway._collect_yaml_env_refs(cfg, env)
        assert result == []

    def test_reads_from_dotenv_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("api_key: os.environ/DOTENV_ONLY_TOKEN\n")
        monkeypatch.delenv("DOTENV_ONLY_TOKEN", raising=False)

        # .env as sibling of config file — no CWD dependency
        (tmp_path / ".env").write_text("DOTENV_ONLY_TOKEN=from-dotenv\n")

        env = LiteLLMGateway._resolve_env(cfg)
        result = LiteLLMGateway._collect_yaml_env_refs(cfg, env)
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

        calls: list[list[str]] = []

        def fake_docker(*args: str, check: bool = True, timeout: int = 30):
            calls.append(list(args))
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

        litellm_run = " ".join(next(c for c in calls if "LITELLM_MASTER_KEY" in " ".join(c)))
        assert "OPENAI_API_KEY_TEST=sk-test" in litellm_run  # pragma: allowlist secret

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
        calls: list[list[str]] = []

        def fake_docker(*args: str, check: bool = True, timeout: int = 30):
            calls.append(list(args))
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

        litellm_run = " ".join(next(c for c in calls if "LITELLM_MASTER_KEY" in " ".join(c)))
        assert "CHATGPT_TOKEN_DIR=/app/data/chatgpt" in litellm_run

    @pytest.mark.asyncio
    async def test_start_requires_phoenix_endpoint_when_callback_enabled(self, tmp_path: Path):
        """Phoenix tracing is a source-of-truth dependency, not a best-effort sink."""
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text('litellm_settings:\n  callbacks: ["arize_phoenix"]\n')

        gw = LiteLLMGateway(config_path=str(cfg), data_dir=tmp_path, **_LITELLM_KWARGS)

        with (
            patch("pynchy.host.container_manager.docker.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch.object(gw, "_start_postgres", new_callable=AsyncMock),
            pytest.raises(RuntimeError, match="PHOENIX_COLLECTOR_HTTP_ENDPOINT"),
        ):
            await gw.start()

    @pytest.mark.asyncio
    async def test_start_forwards_phoenix_env_and_content_capture(self, tmp_path: Path):
        """Phoenix callback env is forwarded even when not referenced by os.environ/ YAML."""
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text('litellm_settings:\n  callbacks: ["arize_phoenix"]\n')
        (tmp_path / ".env").write_text(
            "PHOENIX_COLLECTOR_HTTP_ENDPOINT=https://phoenix.example.test/v1/traces\n"
            "PHOENIX_PROJECT_NAME=pynchy-test\n"  # pragma: allowlist secret
        )

        gw = LiteLLMGateway(config_path=str(cfg), data_dir=tmp_path, **_LITELLM_KWARGS)
        calls: list[list[str]] = []

        def fake_docker(*args: str, check: bool = True, timeout: int = 30):
            calls.append(list(args))
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

        litellm_run = " ".join(next(c for c in calls if "LITELLM_MASTER_KEY" in " ".join(c)))
        assert (
            "PHOENIX_COLLECTOR_HTTP_ENDPOINT=https://phoenix.example.test/v1/traces" in litellm_run
        )
        assert "PHOENIX_PROJECT_NAME=pynchy-test" in litellm_run
        assert "LITELLM_OTEL_V2=true" in litellm_run
        assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT" in litellm_run


class TestPrepareLiteLLMConfig:
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
    def _fake_docker_recorder(calls: list[list[str]]):
        def fake_docker(*args: str, check: bool = True, timeout: int = 30):
            calls.append(list(args))
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
        assert "DATABASE_URL=" in litellm_run
        assert "postgresql://" in litellm_run
        assert "LITELLM_SALT_KEY=" in litellm_run
        assert "--network pynchy-litellm-net" in litellm_run
        assert wait_healthy_mock.await_args.args[1] == "http://localhost:4000/health/readiness"
        assert isinstance(wait_healthy_mock.await_args.kwargs["health_timeout_seconds"], float)

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
    async def test_start_creates_network_and_postgres(self, gw: LiteLLMGateway):
        """Verify start creates network, Postgres, then LiteLLM."""
        calls: list[list[str]] = []

        with (
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(
                f"{_LITELLM_MOD}.run_docker",
                new_callable=AsyncMock,
                side_effect=self._fake_docker_recorder(calls),
            ),
            patch(
                f"{_DOCKER_MOD}.run_docker",
                new_callable=AsyncMock,
                side_effect=self._fake_docker_recorder(calls),
            ),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch.object(gw, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.wait_healthy", new_callable=AsyncMock) as wait_healthy_mock,
        ):
            await gw.start()

        self._assert_start_calls(calls, wait_healthy_mock)

    @pytest.mark.asyncio
    async def test_stop_removes_all_containers_and_network(self, gw: LiteLLMGateway):
        calls: list[list[str]] = []

        def fake_docker(*args: str, check: bool = True, timeout: int = 30):
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


# ---------------------------------------------------------------------------
# BuiltinGateway — basic tests
# ---------------------------------------------------------------------------


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


class TestBuiltinGatewayAuthHeaders:
    """Builtin gateway uses provider-native API-key headers."""

    @staticmethod
    def _make_gateway(provider: str) -> BuiltinGateway:
        gw = BuiltinGateway(
            port=4010, host=ALL_INTERFACE_BIND_HOST, container_host="host.docker.internal"
        )
        gw._credentials = {
            provider: {"type": "api_key", "value": "sk-secret"},
        }
        return gw

    @staticmethod
    def _make_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
        return headers or {}

    def test_anthropic_creds_use_x_api_key(self):
        gw = self._make_gateway("anthropic")
        headers = gw._build_upstream_headers(self._make_headers(), "anthropic")
        assert headers["x-api-key"] == "sk-secret"
        assert "Authorization" not in headers
        assert "anthropic-beta" not in headers

    def test_openai_creds_use_bearer_auth(self):
        gw = self._make_gateway("openai")
        headers = gw._build_upstream_headers(self._make_headers(), "openai")
        assert headers["Authorization"] == "Bearer sk-secret"
        assert "x-api-key" not in headers


# ---------------------------------------------------------------------------
# Module-level start/stop — mode selection
# ---------------------------------------------------------------------------


class TestGatewayModeSelection:
    @pytest.fixture(autouse=True)
    async def _cleanup(self):
        yield
        # Reset the module-level singleton directly instead of calling
        # stop_gateway(), which would invoke real Docker commands.
        _gw_mod._gateway = None

    @pytest.mark.asyncio
    async def test_litellm_mode_when_config_set(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        from pynchy.config.models import AgentConfig, GatewayConfig

        mock_settings = make_settings(
            agent=AgentConfig(default_core="codex", model="gpt-5.5"),
            gateway=GatewayConfig(
                litellm_config=str(cfg),
                port=4000,
                container_host="host.docker.internal",
                litellm_image="ghcr.io/berriai/litellm:main-latest",
                postgres_image="postgres:17-alpine",
                master_key=SecretStr("test-key"),
            ),
            data_dir=tmp_path,
            mcp_servers={},  # No MCP servers → skip McpManager
        )

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()
            assert isinstance(gw, LiteLLMGateway)
            assert gw._required_models == ("gpt-5.5",)

    @pytest.mark.asyncio
    async def test_default_container_host_resolves_for_apple_runtime(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        from pynchy.config.models import GatewayConfig

        mock_settings = make_settings(
            gateway=GatewayConfig(
                litellm_config=str(cfg),
                port=4000,
                container_host="host.docker.internal",
                litellm_image="ghcr.io/berriai/litellm:main-latest",
                postgres_image="postgres:17-alpine",
                master_key=SecretStr("test-key"),
            ),
            data_dir=tmp_path,
            mcp_servers={},
        )
        runtime = MagicMock()
        runtime.name = "apple"

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch("pynchy.plugins.runtimes.detection.get_runtime", return_value=runtime),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()

        assert gw.base_url == "http://192.168.64.1:4000"

    @pytest.mark.asyncio
    async def test_custom_container_host_is_respected_for_apple_runtime(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        from pynchy.config.models import GatewayConfig

        mock_settings = make_settings(
            gateway=GatewayConfig(
                litellm_config=str(cfg),
                port=4000,
                container_host="pynchy-host.local",
                litellm_image="ghcr.io/berriai/litellm:main-latest",
                postgres_image="postgres:17-alpine",
                master_key=SecretStr("test-key"),
            ),
            data_dir=tmp_path,
            mcp_servers={},
        )
        runtime = MagicMock()
        runtime.name = "apple"

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch("pynchy.plugins.runtimes.detection.get_runtime", return_value=runtime),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()

        assert gw.base_url == "http://pynchy-host.local:4000"

    @pytest.mark.asyncio
    async def test_builtin_mode_when_no_config(self, tmp_path: Path):
        from pynchy.config.models import GatewayConfig

        mock_settings = make_settings(
            gateway=GatewayConfig(
                litellm_config=None,
                port=4010,
                host=ALL_INTERFACE_BIND_HOST,
                container_host="host.docker.internal",
            )
        )

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch.object(BuiltinGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()
            assert isinstance(gw, BuiltinGateway)
