"""Tests for plugin verification — SHA cache, verdict parsing, and categorization."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pynchy.config import PluginConfig
from pynchy.plugin.verifier import (
    _parse_verdict,
    audit_unverified_plugins,
    get_already_verified,
    get_cached_verdict,
    get_unverified,
    store_verdict,
)

# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


class TestParseVerdict:
    def test_pass_verdict(self) -> None:
        text = (
            "The plugin looks safe.\n\n"
            "VERDICT: PASS\n"
            "REASONING: Simple utility plugin with no network calls."
        )
        verdict, reasoning = _parse_verdict(text)
        assert verdict == "pass"
        assert "Simple utility" in reasoning

    def test_fail_verdict(self) -> None:
        text = "Found issues.\nVERDICT: FAIL\nREASONING: Module-level subprocess call phones home."
        verdict, reasoning = _parse_verdict(text)
        assert verdict == "fail"
        assert "subprocess" in reasoning

    def test_defaults_to_error_on_garbage(self) -> None:
        """No VERDICT line → error (agent didn't complete inspection)."""
        verdict, reasoning = _parse_verdict("totally random output with no structure")
        assert verdict == "error"

    def test_defaults_to_error_on_empty(self) -> None:
        verdict, reasoning = _parse_verdict("")
        assert verdict == "error"
        assert "No response" in reasoning

    def test_auth_failure_returns_error(self) -> None:
        """Auth errors from the LLM gateway should not be mistaken for a fail verdict."""
        text = (
            'Failed to authenticate. API Error: 401 {"type":"error",'
            '"error":{"type":"authentication_error","message":"OAuth '
            'authentication is currently not supported."}}'
        )
        verdict, reasoning = _parse_verdict(text)
        assert verdict == "error"

    def test_case_insensitive_verdict(self) -> None:
        text = "verdict: Pass\nreasoning: All good"
        verdict, reasoning = _parse_verdict(text)
        assert verdict == "pass"
        assert reasoning == "All good"

    def test_verdict_at_end_of_long_output(self) -> None:
        """Verdict should be found even after long analysis text."""
        text = (
            "Analysis:\n"
            + "This line is filler.\n" * 50
            + "VERDICT: FAIL\nREASONING: Bad code found."
        )
        verdict, reasoning = _parse_verdict(text)
        assert verdict == "fail"
        assert "Bad code" in reasoning


# ---------------------------------------------------------------------------
# SHA cache database
# ---------------------------------------------------------------------------


class TestVerificationCache:
    def test_store_and_retrieve(self, tmp_path: Path) -> None:
        store_verdict(tmp_path, "my-plugin", "abc123", "pass", "Looks safe", "agent")
        cached = get_cached_verdict(tmp_path, "my-plugin", "abc123")
        assert cached is not None
        assert cached == ("pass", "Looks safe")

    def test_cache_miss(self, tmp_path: Path) -> None:
        assert get_cached_verdict(tmp_path, "nonexistent", "deadbeef") is None

    def test_different_sha_is_cache_miss(self, tmp_path: Path) -> None:
        store_verdict(tmp_path, "my-plugin", "abc123", "pass", "Safe", "agent")
        assert get_cached_verdict(tmp_path, "my-plugin", "def456") is None

    def test_upsert_on_same_sha(self, tmp_path: Path) -> None:
        store_verdict(tmp_path, "my-plugin", "abc123", "pass", "Initially safe", "agent")
        store_verdict(tmp_path, "my-plugin", "abc123", "fail", "Found issues", "agent")
        cached = get_cached_verdict(tmp_path, "my-plugin", "abc123")
        assert cached == ("fail", "Found issues")


# ---------------------------------------------------------------------------
# Plugin categorization (get_already_verified / get_unverified)
# ---------------------------------------------------------------------------


class TestPluginCategorization:
    def test_trusted_is_already_verified(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()
        synced = {"p": (d, "sha1", True)}
        result = get_already_verified(synced, tmp_path)
        assert "p" in result

    def test_cached_pass_is_already_verified(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()
        store_verdict(tmp_path, "p", "sha1", "pass", "Safe")
        synced = {"p": (d, "sha1", False)}
        result = get_already_verified(synced, tmp_path)
        assert "p" in result

    def test_cached_fail_is_not_already_verified(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()
        store_verdict(tmp_path, "p", "sha1", "fail", "Bad")
        synced = {"p": (d, "sha1", False)}
        result = get_already_verified(synced, tmp_path)
        assert "p" not in result

    def test_uncached_is_not_already_verified(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()
        synced = {"p": (d, "sha1", False)}
        result = get_already_verified(synced, tmp_path)
        assert "p" not in result

    def test_trusted_is_not_unverified(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()
        synced = {"p": (d, "sha1", True)}
        result = get_unverified(synced, tmp_path)
        assert "p" not in result

    def test_uncached_untrusted_is_unverified(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()
        synced = {"p": (d, "sha1", False)}
        result = get_unverified(synced, tmp_path)
        assert "p" in result

    def test_cached_is_not_unverified(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()
        store_verdict(tmp_path, "p", "sha1", "pass", "Safe")
        synced = {"p": (d, "sha1", False)}
        result = get_unverified(synced, tmp_path)
        assert "p" not in result

    def test_cached_fail_is_not_unverified(self, tmp_path: Path) -> None:
        """A failed verdict is still cached — don't re-audit the same SHA."""
        d = tmp_path / "p"
        d.mkdir()
        store_verdict(tmp_path, "p", "sha1", "fail", "Bad")
        synced = {"p": (d, "sha1", False)}
        result = get_unverified(synced, tmp_path)
        assert "p" not in result

    def test_mixed_plugins(self, tmp_path: Path) -> None:
        dirs = {}
        for name in ("trusted", "cached", "uncached"):
            d = tmp_path / name
            d.mkdir()
            dirs[name] = d

        store_verdict(tmp_path, "cached", "sha2", "pass", "Safe")

        synced = {
            "trusted": (dirs["trusted"], "sha1", True),
            "cached": (dirs["cached"], "sha2", False),
            "uncached": (dirs["uncached"], "sha3", False),
        }

        verified = get_already_verified(synced, tmp_path)
        assert set(verified) == {"trusted", "cached"}

        unverified = get_unverified(synced, tmp_path)
        assert set(unverified) == {"uncached"}


# ---------------------------------------------------------------------------
# audit_unverified_plugins
# ---------------------------------------------------------------------------


class TestAuditUnverifiedPlugins:
    async def test_stores_verdict_and_returns_passed(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()
        (d / "main.py").write_text("print('hello')")

        unverified = {"p": (d, "sha1")}

        with patch(
            "pynchy.plugin.verifier._audit_single_plugin",
            new_callable=AsyncMock,
            return_value=("pass", "Clean plugin"),
        ):
            result = await audit_unverified_plugins(unverified, tmp_path)

        assert "p" in result
        cached = get_cached_verdict(tmp_path, "p", "sha1")
        assert cached == ("pass", "Clean plugin")

    async def test_failed_plugin_not_returned(self, tmp_path: Path) -> None:
        d = tmp_path / "p"
        d.mkdir()

        unverified = {"p": (d, "sha1")}

        with patch(
            "pynchy.plugin.verifier._audit_single_plugin",
            new_callable=AsyncMock,
            return_value=("fail", "Found malicious patterns"),
        ):
            result = await audit_unverified_plugins(unverified, tmp_path)

        assert "p" not in result
        cached = get_cached_verdict(tmp_path, "p", "sha1")
        assert cached is not None
        assert cached[0] == "fail"

    async def test_exception_not_cached(self, tmp_path: Path) -> None:
        """Infrastructure errors should NOT be cached — retry next boot."""
        d = tmp_path / "p"
        d.mkdir()

        unverified = {"p": (d, "sha1")}

        with patch(
            "pynchy.plugin.verifier._audit_single_plugin",
            new_callable=AsyncMock,
            side_effect=RuntimeError("container crashed"),
        ):
            result = await audit_unverified_plugins(unverified, tmp_path)

        assert "p" not in result
        # Must NOT be cached — so it gets retried next boot
        assert get_cached_verdict(tmp_path, "p", "sha1") is None

    async def test_error_verdict_not_cached(self, tmp_path: Path) -> None:
        """When the agent runs but produces no VERDICT line (e.g. auth failure)."""
        d = tmp_path / "p"
        d.mkdir()

        unverified = {"p": (d, "sha1")}

        with patch(
            "pynchy.plugin.verifier._audit_single_plugin",
            new_callable=AsyncMock,
            return_value=("error", "Auth failure: 401"),
        ):
            result = await audit_unverified_plugins(unverified, tmp_path)

        assert "p" not in result
        assert get_cached_verdict(tmp_path, "p", "sha1") is None

    async def test_mixed_results(self, tmp_path: Path) -> None:
        good_dir = tmp_path / "good"
        good_dir.mkdir()
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()

        unverified = {"good": (good_dir, "sha1"), "bad": (bad_dir, "sha2")}

        async def mock_audit(name: str, _dir: Path, _pm: None = None) -> tuple[str, str]:
            if name == "good":
                return ("pass", "Clean")
            return ("fail", "Malicious")

        with patch(
            "pynchy.plugin.verifier._audit_single_plugin",
            side_effect=mock_audit,
        ):
            result = await audit_unverified_plugins(unverified, tmp_path)

        assert set(result) == {"good"}


# ---------------------------------------------------------------------------
# plugin_sync split
# ---------------------------------------------------------------------------


class TestPluginSyncSplit:
    def test_sync_plugin_repos_returns_trusted_flag(self, tmp_path: Path) -> None:
        from pynchy.plugin.sync import sync_plugin_repos

        settings = SimpleNamespace(
            plugins_dir=tmp_path / "plugins",
            plugins={
                "trusted": PluginConfig(repo="user/repo", ref="main", enabled=True, trusted=True),
                "untrusted": PluginConfig(
                    repo="user/repo2", ref="main", enabled=True, trusted=False
                ),
                "disabled": PluginConfig(repo="user/repo3", ref="main", enabled=False),
            },
        )

        trusted_dir = settings.plugins_dir / "trusted"
        untrusted_dir = settings.plugins_dir / "untrusted"

        with (
            patch("pynchy.plugin.sync.get_settings", return_value=settings),
            patch(
                "pynchy.plugin.sync._sync_single_plugin",
                side_effect=lambda name, cfg, root: root / name,
            ),
            patch("pynchy.plugin.sync._plugin_revision", return_value="abc123"),
        ):
            result = sync_plugin_repos()

        assert "trusted" in result
        assert "untrusted" in result
        assert "disabled" not in result
        assert result["trusted"] == (trusted_dir, "abc123", True)
        assert result["untrusted"] == (untrusted_dir, "abc123", False)

    def test_install_verified_plugins_only_installs_verified(self, tmp_path: Path) -> None:
        from pynchy.plugin.sync import install_verified_plugins

        settings = SimpleNamespace(plugins_dir=tmp_path / "plugins")
        settings.plugins_dir.mkdir(parents=True)

        verified = {
            "good": (tmp_path / "good", "sha1"),
        }

        with (
            patch("pynchy.plugin.sync.get_settings", return_value=settings),
            patch("pynchy.plugin.sync._load_install_state", return_value={}),
            patch("pynchy.plugin.sync._save_install_state"),
            patch("pynchy.plugin.sync._install_plugin_in_host_env") as mock_install,
        ):
            result = install_verified_plugins(verified)

        assert "good" in result
        mock_install.assert_called_once_with("good", tmp_path / "good")
