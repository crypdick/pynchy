"""Tests for src/pynchy/tunnels/ and src/pynchy/plugin/builtin_tailscale.py.

Unit tests for provider logic and consumer code, plus integration tests
that verify the plugin is auto-discovered by get_plugin_manager().
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests need TimeoutExpired, not process execution.
from unittest.mock import MagicMock, patch

import pluggy

from pynchy.plugins import get_plugin_manager
from pynchy.plugins.tunnels.api import TunnelProvider, check_tunnels


def _get_pm():
    with patch(
        "pluggy.PluginManager.load_setuptools_entrypoints",
        return_value=0,
    ):
        return get_plugin_manager()


def _tailscale_provider() -> TunnelProvider:
    providers = _get_pm().hook.pynchy_tunnel()
    return next(
        provider for provider in providers if getattr(provider, "name", None) == "tailscale"
    )


# ---------------------------------------------------------------------------
# TunnelProvider validation
# ---------------------------------------------------------------------------


class TestTunnelProviderContract:
    """Test provider acceptance through public tunnel behavior."""

    def test_tailscale_provider_satisfies_protocol(self):
        assert isinstance(_tailscale_provider(), TunnelProvider)

    def test_check_tunnels_skips_provider_missing_method(self):
        """Object missing required methods should be skipped."""

        class Incomplete:
            name = "broken"

            def is_available(self) -> bool:
                return True

        pm = TestCheckTunnels._make_pm([Incomplete()])
        check_tunnels(pm)

    def test_check_tunnels_skips_provider_with_non_callable_methods(self):
        """Object with non-callable attributes should be skipped."""

        class BadProvider:
            name = "bad"
            is_available = True  # not callable
            is_connected = True
            status_summary = "nope"

        pm = TestCheckTunnels._make_pm([BadProvider()])
        check_tunnels(pm)


# ---------------------------------------------------------------------------
# _TailscaleTunnel
# ---------------------------------------------------------------------------


class TestTailscaleTunnel:
    """Test the built-in Tailscale tunnel provider."""

    def test_is_available_found(self):
        with patch(
            "pynchy.plugins.tunnels.tailscale.shutil.which",
            return_value="/usr/bin/tailscale",
        ):
            assert _tailscale_provider().is_available()

    def test_is_available_not_found(self):
        with patch("pynchy.plugins.tunnels.tailscale.shutil.which", return_value=None):
            assert not _tailscale_provider().is_available()

    def test_is_connected_running(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"BackendState": "Running"})

        with patch("pynchy.plugins.tunnels.tailscale.subprocess.run", return_value=mock_result):
            t = _tailscale_provider()
            assert t.is_connected()
            assert t.status_summary() == "BackendState=Running"

    def test_is_connected_stopped(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"BackendState": "Stopped"})

        with patch("pynchy.plugins.tunnels.tailscale.subprocess.run", return_value=mock_result):
            t = _tailscale_provider()
            assert not t.is_connected()
            assert t.status_summary() == "BackendState=Stopped"

    def test_is_connected_cli_fails(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("pynchy.plugins.tunnels.tailscale.subprocess.run", return_value=mock_result):
            t = _tailscale_provider()
            assert not t.is_connected()
            assert "exit code" in t.status_summary()

    def test_is_connected_not_installed(self):
        with patch(
            "pynchy.plugins.tunnels.tailscale.subprocess.run",
            side_effect=FileNotFoundError("No such file"),
        ):
            t = _tailscale_provider()
            assert not t.is_connected()
            assert t.status_summary() == "CLI not found"

    def test_is_connected_timeout(self):
        with patch(
            "pynchy.plugins.tunnels.tailscale.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=5),
        ):
            t = _tailscale_provider()
            assert not t.is_connected()
            assert "timed out" in t.status_summary().lower()

    def test_missing_backend_state(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"Health": "ok"})

        with patch("pynchy.plugins.tunnels.tailscale.subprocess.run", return_value=mock_result):
            t = _tailscale_provider()
            assert not t.is_connected()
            assert "unknown" in t.status_summary()

    def test_caches_subprocess_result(self):
        """is_connected() and status_summary() should only call subprocess once."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"BackendState": "Running"})

        with patch(
            "pynchy.plugins.tunnels.tailscale.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            t = _tailscale_provider()
            t.is_connected()
            t.status_summary()
            mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# check_tunnels
# ---------------------------------------------------------------------------


class _FakePM(pluggy.PluginManager):
    """Real-class stand-in so isinstance(pm, pluggy.PluginManager) succeeds."""

    def __init__(self, hook: MagicMock) -> None:
        self.hook = hook


class _FakeTunnel:
    """Concrete protocol-shaped tunnel test double."""

    def __init__(
        self,
        *,
        name: str = "test",
        available: bool = True,
        connected: bool = True,
        available_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.available = available
        self.connected = connected
        self.available_error = available_error
        self.is_available_calls = 0
        self.is_connected_calls = 0
        self.status_summary_calls = 0

    def is_available(self) -> bool:
        self.is_available_calls += 1
        if self.available_error is not None:
            raise self.available_error
        return self.available

    def is_connected(self) -> bool:
        self.is_connected_calls += 1
        return self.connected

    def status_summary(self) -> str:
        self.status_summary_calls += 1
        return "ok" if self.connected else "disconnected"


class TestCheckTunnels:
    """Test the check_tunnels() consumer function."""

    @staticmethod
    def _make_pm(tunnel_returns: list) -> _FakePM:
        hook = MagicMock()
        hook.pynchy_tunnel.return_value = tunnel_returns
        return _FakePM(hook)

    @staticmethod
    def _make_tunnel(
        *, name: str = "test", available: bool = True, connected: bool = True
    ) -> _FakeTunnel:
        return _FakeTunnel(name=name, available=available, connected=connected)

    def test_no_tunnel_plugins(self):
        pm = self._make_pm([])
        check_tunnels(pm)  # Should not raise

    def test_one_tunnel_connected(self):
        tunnel = self._make_tunnel(connected=True)
        pm = self._make_pm([tunnel])
        check_tunnels(pm)  # Should not raise
        assert tunnel.is_connected_calls == 1

    def test_one_tunnel_disconnected(self):
        tunnel = self._make_tunnel(connected=False)
        pm = self._make_pm([tunnel])
        check_tunnels(pm)  # Should not raise (warning only)
        assert tunnel.is_connected_calls == 1

    def test_tunnel_not_available(self):
        tunnel = self._make_tunnel(available=False)
        pm = self._make_pm([tunnel])
        with patch("pynchy.plugins.tunnels.api.logger.warning") as warning:
            check_tunnels(pm)
        assert tunnel.is_connected_calls == 0
        warning.assert_not_called()

    def test_tunnel_check_exception(self):
        tunnel = _FakeTunnel(available_error=RuntimeError("boom"))
        pm = self._make_pm([tunnel])
        check_tunnels(pm)  # Should not raise

    def test_none_results_filtered(self):
        tunnel = self._make_tunnel()
        pm = self._make_pm([None, tunnel, None])
        check_tunnels(pm)
        assert tunnel.is_connected_calls == 1

    def test_invalid_provider_skipped(self):
        valid = self._make_tunnel(name="good")
        invalid = "not a tunnel"  # string, not a provider
        pm = self._make_pm([invalid, valid])
        check_tunnels(pm)  # Should not raise
        assert valid.is_connected_calls == 1

    def test_hook_exception_handled(self):
        hook = MagicMock()
        hook.pynchy_tunnel.side_effect = RuntimeError("plugin crash")
        pm = _FakePM(hook)
        check_tunnels(pm)  # Should not raise


# ---------------------------------------------------------------------------
# Integration: plugin discovery via get_plugin_manager()
# ---------------------------------------------------------------------------


class TestTailscalePluginIntegration:
    """Verify builtin_tailscale.py is auto-discovered by the plugin manager."""

    @staticmethod
    def _get_pm():
        return _get_pm()

    def test_tailscale_plugin_registered(self):
        """TailscaleTunnelPlugin appears in the plugin manager's registry."""
        pm = self._get_pm()
        names = [pm.get_name(p) for p in pm.get_plugins()]
        assert "builtin-tailscale" in names

    def test_pynchy_tunnel_hook_returns_provider(self):
        """pynchy_tunnel hook returns a valid TunnelProvider from Tailscale."""
        pm = self._get_pm()
        results = pm.hook.pynchy_tunnel()
        assert len(results) >= 1

        tailscale = next(
            (r for r in results if getattr(r, "name", None) == "tailscale"),
            None,
        )
        assert tailscale is not None
        assert isinstance(tailscale, TunnelProvider)

    def test_check_tunnels_with_real_pm(self):
        """check_tunnels() works with the real plugin manager (mocked subprocess)."""
        pm = self._get_pm()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"BackendState": "Running"})

        with (
            patch(
                "pynchy.plugins.tunnels.tailscale.subprocess.run",
                return_value=mock_result,
            ),
            patch(
                "pynchy.plugins.tunnels.tailscale.shutil.which",
                return_value="/usr/bin/tailscale",
            ),
        ):
            check_tunnels(pm)  # Should not raise
