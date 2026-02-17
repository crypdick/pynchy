"""Tests for src/pynchy/tunnels/ and src/pynchy/plugin/builtin_tailscale.py.

Unit tests for provider logic and consumer code, plus integration tests
that verify the plugin is auto-discovered by get_plugin_manager().
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from pynchy.tunnels import TunnelProvider, _is_valid_tunnel_provider, check_tunnels
from pynchy.tunnels.plugins.tailscale import _TailscaleTunnel

# ---------------------------------------------------------------------------
# TunnelProvider validation
# ---------------------------------------------------------------------------


class TestTunnelValidation:
    """Test the _is_valid_tunnel_provider helper."""

    def test_valid_provider_accepted(self):
        tunnel = _TailscaleTunnel()
        assert _is_valid_tunnel_provider(tunnel)

    def test_missing_method_rejected(self):
        """Object missing required methods should be rejected."""

        class Incomplete:
            name = "broken"

            def is_available(self) -> bool:
                return True

        assert not _is_valid_tunnel_provider(Incomplete())

    def test_non_callable_rejected(self):
        """Object with non-callable attributes should be rejected."""

        class BadProvider:
            name = "bad"
            is_available = True  # not callable
            is_connected = True
            status_summary = "nope"

        assert not _is_valid_tunnel_provider(BadProvider())

    def test_protocol_runtime_check(self):
        """_TailscaleTunnel should satisfy the Protocol at runtime."""
        assert isinstance(_TailscaleTunnel(), TunnelProvider)


# ---------------------------------------------------------------------------
# _TailscaleTunnel
# ---------------------------------------------------------------------------


class TestTailscaleTunnel:
    """Test the built-in Tailscale tunnel provider."""

    def test_is_available_found(self):
        with patch(
            "pynchy.tunnels.plugins.tailscale.shutil.which",
            return_value="/usr/bin/tailscale",
        ):
            assert _TailscaleTunnel().is_available()

    def test_is_available_not_found(self):
        with patch("pynchy.tunnels.plugins.tailscale.shutil.which", return_value=None):
            assert not _TailscaleTunnel().is_available()

    def test_is_connected_running(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"BackendState": "Running"})

        with patch("pynchy.tunnels.plugins.tailscale.subprocess.run", return_value=mock_result):
            t = _TailscaleTunnel()
            assert t.is_connected()
            assert t.status_summary() == "BackendState=Running"

    def test_is_connected_stopped(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"BackendState": "Stopped"})

        with patch("pynchy.tunnels.plugins.tailscale.subprocess.run", return_value=mock_result):
            t = _TailscaleTunnel()
            assert not t.is_connected()
            assert t.status_summary() == "BackendState=Stopped"

    def test_is_connected_cli_fails(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("pynchy.tunnels.plugins.tailscale.subprocess.run", return_value=mock_result):
            t = _TailscaleTunnel()
            assert not t.is_connected()
            assert "exit code" in t.status_summary()

    def test_is_connected_not_installed(self):
        with patch(
            "pynchy.tunnels.plugins.tailscale.subprocess.run",
            side_effect=FileNotFoundError("No such file"),
        ):
            t = _TailscaleTunnel()
            assert not t.is_connected()
            assert t.status_summary() == "CLI not found"

    def test_is_connected_timeout(self):
        import subprocess

        with patch(
            "pynchy.tunnels.plugins.tailscale.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=5),
        ):
            t = _TailscaleTunnel()
            assert not t.is_connected()
            assert "timed out" in t.status_summary().lower()

    def test_missing_backend_state(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"Health": "ok"})

        with patch("pynchy.tunnels.plugins.tailscale.subprocess.run", return_value=mock_result):
            t = _TailscaleTunnel()
            assert not t.is_connected()
            assert "unknown" in t.status_summary()

    def test_caches_subprocess_result(self):
        """is_connected() and status_summary() should only call subprocess once."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"BackendState": "Running"})

        with patch(
            "pynchy.tunnels.plugins.tailscale.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            t = _TailscaleTunnel()
            t.is_connected()
            t.status_summary()
            mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# check_tunnels
# ---------------------------------------------------------------------------


class TestCheckTunnels:
    """Test the check_tunnels() consumer function."""

    @staticmethod
    def _make_pm(tunnel_returns: list) -> MagicMock:
        pm = MagicMock()
        pm.hook.pynchy_tunnel.return_value = tunnel_returns
        return pm

    @staticmethod
    def _make_tunnel(
        *, name: str = "test", available: bool = True, connected: bool = True
    ) -> MagicMock:
        t = MagicMock()
        t.name = name
        t.is_available.return_value = available
        t.is_connected.return_value = connected
        t.status_summary.return_value = "ok" if connected else "disconnected"
        return t

    def test_no_tunnel_plugins(self):
        pm = self._make_pm([])
        check_tunnels(pm)  # Should not raise

    def test_one_tunnel_connected(self):
        tunnel = self._make_tunnel(connected=True)
        pm = self._make_pm([tunnel])
        check_tunnels(pm)  # Should not raise
        tunnel.is_connected.assert_called_once()

    def test_one_tunnel_disconnected(self):
        tunnel = self._make_tunnel(connected=False)
        pm = self._make_pm([tunnel])
        check_tunnels(pm)  # Should not raise (warning only)
        tunnel.is_connected.assert_called_once()

    def test_tunnel_not_available(self):
        tunnel = self._make_tunnel(available=False)
        pm = self._make_pm([tunnel])
        check_tunnels(pm)  # Should not raise
        tunnel.is_connected.assert_not_called()

    def test_tunnel_check_exception(self):
        tunnel = self._make_tunnel()
        tunnel.is_available.side_effect = RuntimeError("boom")
        pm = self._make_pm([tunnel])
        check_tunnels(pm)  # Should not raise

    def test_none_results_filtered(self):
        tunnel = self._make_tunnel()
        pm = self._make_pm([None, tunnel, None])
        check_tunnels(pm)
        tunnel.is_connected.assert_called_once()

    def test_invalid_provider_skipped(self):
        valid = self._make_tunnel(name="good")
        invalid = "not a tunnel"  # string, not a provider
        pm = self._make_pm([invalid, valid])
        check_tunnels(pm)  # Should not raise
        valid.is_connected.assert_called_once()

    def test_hook_exception_handled(self):
        pm = MagicMock()
        pm.hook.pynchy_tunnel.side_effect = RuntimeError("plugin crash")
        check_tunnels(pm)  # Should not raise


# ---------------------------------------------------------------------------
# Integration: plugin discovery via get_plugin_manager()
# ---------------------------------------------------------------------------


class TestTailscalePluginIntegration:
    """Verify builtin_tailscale.py is auto-discovered by the plugin manager."""

    @staticmethod
    def _get_pm():
        from pynchy.plugin import get_plugin_manager

        with patch(
            "pluggy.PluginManager.load_setuptools_entrypoints",
            return_value=0,
        ):
            return get_plugin_manager()

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
        assert _is_valid_tunnel_provider(tailscale)

    def test_check_tunnels_with_real_pm(self):
        """check_tunnels() works with the real plugin manager (mocked subprocess)."""
        pm = self._get_pm()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"BackendState": "Running"})

        with (
            patch(
                "pynchy.tunnels.plugins.tailscale.subprocess.run",
                return_value=mock_result,
            ),
            patch(
                "pynchy.tunnels.plugins.tailscale.shutil.which",
                return_value="/usr/bin/tailscale",
            ),
        ):
            check_tunnels(pm)  # Should not raise
