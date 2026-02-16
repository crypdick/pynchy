"""Tests for the LLM gateway — LiteLLM and Builtin modes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.gateway import (
    BuiltinGateway,
    LiteLLMGateway,
    start_gateway,
    stop_gateway,
)

# ---------------------------------------------------------------------------
# LiteLLMGateway — unit tests (Docker calls mocked)
# ---------------------------------------------------------------------------

_GATEWAY_MOD = "pynchy.gateway"


class TestLiteLLMGatewayInit:
    def test_generates_ephemeral_key(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            port=4000,
            container_host="host.docker.internal",
            image="ghcr.io/berriai/litellm:main-latest",
            data_dir=tmp_path,
        )
        assert gw.key.startswith("sk-pynchy-")
        assert len(gw.key) > 20

    def test_base_url(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            port=4000,
            container_host="host.docker.internal",
            image="ghcr.io/berriai/litellm:main-latest",
            data_dir=tmp_path,
        )
        assert gw.base_url == "http://host.docker.internal:4000"

    def test_has_provider_always_true(self, tmp_path: Path):
        gw = LiteLLMGateway(
            config_path=str(tmp_path / "config.yaml"),
            port=4000,
            container_host="host.docker.internal",
            image="ghcr.io/berriai/litellm:main-latest",
            data_dir=tmp_path,
        )
        assert gw.has_provider("anthropic") is True
        assert gw.has_provider("openai") is True
        assert gw.has_provider("anything") is True


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
            port=4000,
            container_host="host.docker.internal",
            image="ghcr.io/berriai/litellm:main-latest",
            data_dir=tmp_path,
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
            port=4000,
            container_host="host.docker.internal",
            image="ghcr.io/berriai/litellm:main-latest",
            data_dir=tmp_path,
        )
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            pytest.raises(FileNotFoundError, match="LiteLLM config not found"),
        ):
            await gw.start()

    @pytest.mark.asyncio
    async def test_start_issues_docker_run(self, gw: LiteLLMGateway):
        """Verify docker run is called with the right arguments."""
        calls: list[list[str]] = []

        def fake_docker(*args: str, check: bool = True):
            calls.append(list(args))
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            return result

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(type(gw), "_run_docker", staticmethod(fake_docker)),
            patch.object(gw, "_wait_healthy", new_callable=AsyncMock),
        ):
            await gw.start()

        # First call: rm -f stale container
        assert calls[0] == ["rm", "-f", "pynchy-litellm"]

        # Second call: docker run
        run_args = calls[1]
        assert run_args[0] == "run"
        assert "-d" in run_args
        assert "--name" in run_args
        assert "pynchy-litellm" in run_args
        assert any(arg.startswith("LITELLM_MASTER_KEY=") for arg in run_args)
        assert any("config.yaml:ro" in arg for arg in run_args)

    @pytest.mark.asyncio
    async def test_stop_removes_container(self, gw: LiteLLMGateway):
        calls: list[list[str]] = []

        def fake_docker(*args: str, check: bool = True):
            calls.append(list(args))
            result = MagicMock()
            result.stdout = ""
            result.returncode = 0
            return result

        with patch.object(type(gw), "_run_docker", staticmethod(fake_docker)):
            await gw.stop()

        assert ["stop", "-t", "5", "pynchy-litellm"] in calls
        assert ["rm", "-f", "pynchy-litellm"] in calls


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
