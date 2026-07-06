"""Tests for the LLM gateway — LiteLLM and Builtin modes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings
from pydantic import SecretStr

import pynchy.host.container_manager.gateway as _gw_mod
from pynchy.host.container_manager.gateway import (
    BuiltinGateway,
    LiteLLMGateway,
    _load_or_create_persistent_key,  # allow: private-test-imports
    start_gateway,
)

# The anthropic-beta value is an external Anthropic API contract (OAuth token
# requests 401 without it). Pin the literal so a production change trips the test.
_ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"

# ---------------------------------------------------------------------------
# LiteLLMGateway — unit tests (Docker calls mocked)
# ---------------------------------------------------------------------------

_GATEWAY_MOD = "pynchy.host.container_manager.gateway"
_LITELLM_MOD = "pynchy.host.container_manager.gateway_litellm"
_DOCKER_MOD = "pynchy.host.container_manager.docker"

_LITELLM_KWARGS = {
    "port": 4000,
    "container_host": "host.docker.internal",
    "image": "ghcr.io/berriai/litellm:main-latest",
    "postgres_image": "postgres:17-alpine",
    "master_key": "test-master-key",
}


class TestPersistentKey:
    def test_creates_key_on_first_call(self, tmp_path: Path):
        key_file = tmp_path / "test.key"
        key = _load_or_create_persistent_key(key_file, prefix="pfx-")
        assert key.startswith("pfx-")
        assert key_file.exists()
        assert key_file.read_text().strip() == key

    def test_returns_existing_key(self, tmp_path: Path):
        key_file = tmp_path / "test.key"
        key_file.write_text("my-fixed-key")
        key = _load_or_create_persistent_key(key_file, prefix="pfx-")
        assert key == "my-fixed-key"

    def test_creates_parent_dirs(self, tmp_path: Path):
        key_file = tmp_path / "a" / "b" / "test.key"
        key = _load_or_create_persistent_key(key_file)
        assert key_file.exists()
        assert len(key) > 10


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
        cfg.write_text("api_key: os.environ/CLAUDE_OAUTH_TOKEN\n")
        monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-discovered")

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
        assert (
            "CLAUDE_OAUTH_TOKEN=sk-ant-oat01-discovered"  # pragma: allowlist secret
            in litellm_run
        )


class TestLiteLLMGatewayStart:
    @pytest.fixture
    def litellm_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")
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

        def fake_docker(*args: str, check: bool = True, timeout: int = 30):
            calls.append(list(args))
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            return result

        with (
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_DOCKER_MOD}.run_docker", new_callable=AsyncMock, side_effect=fake_docker),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch.object(gw, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.wait_healthy", new_callable=AsyncMock),
        ):
            await gw.start()

        flat = [" ".join(c) for c in calls]

        assert any("pynchy-litellm-db" in c and "run" in c for c in flat)
        assert any("pynchy-litellm" in c and "run" in c and "LITELLM_MASTER_KEY" in c for c in flat)

        litellm_run = next(c for c in flat if "LITELLM_MASTER_KEY" in c)
        assert "DATABASE_URL=" in litellm_run
        assert "postgresql://" in litellm_run
        assert "LITELLM_SALT_KEY=" in litellm_run
        assert "--network pynchy-litellm-net" in litellm_run

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
        gw = BuiltinGateway(port=4010, host="0.0.0.0", container_host="host.docker.internal")
        assert gw.key.startswith("gw-")
        assert len(gw.key) > 20

    def test_base_url(self):
        gw = BuiltinGateway(port=4010, host="0.0.0.0", container_host="host.docker.internal")
        assert gw.base_url == "http://host.docker.internal:4010"

    def test_has_provider_false_before_start(self):
        gw = BuiltinGateway(port=4010, host="0.0.0.0", container_host="host.docker.internal")
        assert gw.has_provider("anthropic") is False
        assert gw.has_provider("openai") is False


class TestBuiltinGatewayOAuthHeader:
    """Lock in the OAuth beta header invariant.

    Without ``anthropic-beta: oauth-2025-04-20``, Anthropic rejects OAuth tokens
    with 401 ``"OAuth authentication is currently not supported."``  This header
    is the sole mechanism converting subscription-OAuth credentials into accepted
    API calls — and the sole reason pynchy containers are subscription-billed
    rather than API-billed. Removing it silently breaks billing.
    """

    @staticmethod
    def _make_gateway(creds_type: str) -> BuiltinGateway:
        gw = BuiltinGateway(port=4010, host="0.0.0.0", container_host="host.docker.internal")
        gw._credentials = {
            "anthropic": {"type": creds_type, "value": "sk-ant-secret"},
        }
        return gw

    @staticmethod
    def _make_request(headers: dict[str, str] | None = None) -> MagicMock:
        request = MagicMock()
        request.headers = headers or {}
        return request

    def test_oauth_creds_inject_beta_header(self):
        gw = self._make_gateway("oauth")
        headers = gw._build_upstream_headers(self._make_request(), "anthropic")
        assert _ANTHROPIC_OAUTH_BETA in headers.get("anthropic-beta", ""), (
            "OAuth token requests must carry the anthropic-beta: oauth-2025-04-20 "
            "header or Anthropic returns 401. See gateway_builtin.py for context."
        )

    def test_oauth_creds_use_bearer_auth(self):
        gw = self._make_gateway("oauth")
        headers = gw._build_upstream_headers(self._make_request(), "anthropic")
        assert headers["Authorization"] == "Bearer sk-ant-secret"
        assert "x-api-key" not in headers

    def test_api_key_creds_do_not_set_beta_header(self):
        gw = self._make_gateway("api_key")
        headers = gw._build_upstream_headers(self._make_request(), "anthropic")
        assert headers["x-api-key"] == "sk-ant-secret"
        assert "Authorization" not in headers
        assert "anthropic-beta" not in headers

    def test_oauth_preserves_existing_beta_flags(self):
        """Clients may send their own anthropic-beta flags; don't clobber them."""
        gw = self._make_gateway("oauth")
        headers = gw._build_upstream_headers(
            self._make_request({"anthropic-beta": "prompt-caching-2024-07-31"}),
            "anthropic",
        )
        beta = headers["anthropic-beta"]
        assert "prompt-caching-2024-07-31" in beta
        assert _ANTHROPIC_OAUTH_BETA in beta

    def test_oauth_beta_not_duplicated_if_already_present(self):
        gw = self._make_gateway("oauth")
        headers = gw._build_upstream_headers(
            self._make_request({"anthropic-beta": _ANTHROPIC_OAUTH_BETA}),
            "anthropic",
        )
        assert headers["anthropic-beta"].count(_ANTHROPIC_OAUTH_BETA) == 1


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
            mcp_servers={},  # No MCP servers → skip McpManager
        )

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()
            assert isinstance(gw, LiteLLMGateway)

    @pytest.mark.asyncio
    async def test_builtin_mode_when_no_config(self, tmp_path: Path):
        from pynchy.config.models import GatewayConfig

        mock_settings = make_settings(
            gateway=GatewayConfig(
                litellm_config=None,
                port=4010,
                host="0.0.0.0",
                container_host="host.docker.internal",
            )
        )

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch.object(BuiltinGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()
            assert isinstance(gw, BuiltinGateway)
