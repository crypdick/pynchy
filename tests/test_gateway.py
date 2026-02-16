"""Tests for the LLM gateway — LiteLLM and Builtin modes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.gateway import (
    BuiltinGateway,
    LiteLLMGateway,
    _load_or_create_persistent_key,
    start_gateway,
    stop_gateway,
)

# ---------------------------------------------------------------------------
# LiteLLMGateway — unit tests (Docker calls mocked)
# ---------------------------------------------------------------------------

_GATEWAY_MOD = "pynchy.gateway"

_LITELLM_KWARGS = dict(
    port=4000,
    container_host="host.docker.internal",
    image="ghcr.io/berriai/litellm:main-latest",
    postgres_image="postgres:17-alpine",
)


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
    def test_generates_ephemeral_key(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            data_dir=tmp_path,
            **_LITELLM_KWARGS,
        )
        assert gw.key.startswith("sk-pynchy-")
        assert len(gw.key) > 20

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
            patch("shutil.which", return_value=None),
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
            patch("shutil.which", return_value="/usr/bin/docker"),
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
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(type(gw), "_run_docker", staticmethod(fake_docker)),
            patch.object(gw, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch.object(gw, "_wait_healthy", new_callable=AsyncMock),
        ):
            await gw.start()

        flat = [" ".join(c) for c in calls]

        assert any("network inspect pynchy-litellm-net" in c for c in flat)
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

        with patch.object(type(gw), "_run_docker", staticmethod(fake_docker)):
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


# ---------------------------------------------------------------------------
# Module-level start/stop — mode selection
# ---------------------------------------------------------------------------


class TestGatewayModeSelection:
    @pytest.fixture(autouse=True)
    async def _cleanup(self):
        yield
        await stop_gateway()

    @pytest.mark.asyncio
    async def test_litellm_mode_when_config_set(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        mock_settings = MagicMock()
        mock_settings.gateway.litellm_config = str(cfg)
        mock_settings.gateway.port = 4000
        mock_settings.gateway.container_host = "host.docker.internal"
        mock_settings.gateway.litellm_image = "ghcr.io/berriai/litellm:main-latest"
        mock_settings.gateway.postgres_image = "postgres:17-alpine"
        mock_settings.data_dir = tmp_path

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()
            assert isinstance(gw, LiteLLMGateway)

    @pytest.mark.asyncio
    async def test_builtin_mode_when_no_config(self, tmp_path: Path):
        mock_settings = MagicMock()
        mock_settings.gateway.litellm_config = None
        mock_settings.gateway.port = 4010
        mock_settings.gateway.host = "0.0.0.0"
        mock_settings.gateway.container_host = "host.docker.internal"

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch.object(BuiltinGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()
            assert isinstance(gw, BuiltinGateway)
