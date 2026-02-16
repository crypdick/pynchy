"""Sync plugin repositories declared in config.toml.

Split into two phases:
  1. sync_plugin_repos()  — clone/update git repos (sync, no install)
  2. install_verified_plugins() — install only verified/trusted plugins

The verification step (plugin_verifier) runs between these two phases.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from pynchy.config import PluginConfig, get_settings
from pynchy.logger import logger

_HEX_REF_RE = re.compile(r"^[0-9a-f]{7,40}$")


def normalize_repo_url(repo: str) -> str:
    """Normalize shorthand plugin repo references to clone URLs."""
    value = repo.strip()
    if "://" in value or value.startswith("git@"):
        return value
    if value.count("/") == 1:
        return f"https://github.com/{value}.git"
    return value


def _run_git(*args: str, cwd: Path | None = None) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr[-500:]}")


def _is_dirty(repo_dir: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return True
    return bool((result.stdout or "").strip())


def _sync_single_plugin(plugin_name: str, plugin_cfg: PluginConfig, root: Path) -> Path:
    repo_url = normalize_repo_url(plugin_cfg.repo)
    plugin_dir = root / plugin_name
    ref = plugin_cfg.ref.strip() or "main"

    if not plugin_dir.exists():
        _run_git(
            "clone",
            "--single-branch",
            "--branch",
            ref,
            repo_url,
            str(plugin_dir),
        )
        return plugin_dir

    if _is_dirty(plugin_dir):
        logger.warning(
            "Plugin repo has local changes, skipping update",
            plugin=plugin_name,
            path=str(plugin_dir),
        )
        return plugin_dir

    _run_git("fetch", "origin", "--tags", "--prune", cwd=plugin_dir)
    _run_git("checkout", ref, cwd=plugin_dir)
    if not _HEX_REF_RE.match(ref):
        _run_git("pull", "--ff-only", "origin", ref, cwd=plugin_dir)
    return plugin_dir


def _plugin_revision(plugin_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(plugin_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"git rev-parse HEAD failed: {stderr[-500:]}")
    return (result.stdout or "").strip()


def _install_state_path(root: Path) -> Path:
    return root / ".host-install-state.json"


def _load_install_state(root: Path) -> dict[str, dict[str, str]]:
    state_path = _install_state_path(root)
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read plugin host install state; resetting")
    return {}


def _save_install_state(root: Path, state: dict[str, dict[str, str]]) -> None:
    state_path = _install_state_path(root)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _install_plugin_in_host_env(plugin_name: str, plugin_dir: Path) -> None:
    result = subprocess.run(
        ["uv", "pip", "install", "--python", sys.executable, "--no-cache-dir", str(plugin_dir)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"Host plugin install failed for '{plugin_name}': {stderr[-500:]}")


# ---------------------------------------------------------------------------
# Phase 1: Clone/update repos only (no installation, no verification)
# ---------------------------------------------------------------------------


def sync_plugin_repos() -> dict[str, tuple[Path, str, bool]]:
    """Clone or update configured plugin repositories.

    Returns:
        Dict of {name: (plugin_dir, git_sha, trusted)} for each enabled plugin.
    """
    s = get_settings()
    s.plugins_dir.mkdir(parents=True, exist_ok=True)
    synced: dict[str, tuple[Path, str, bool]] = {}

    for plugin_name, plugin_cfg in s.plugins.items():
        if not plugin_cfg.enabled:
            continue
        try:
            plugin_dir = _sync_single_plugin(plugin_name, plugin_cfg, s.plugins_dir)
            revision = _plugin_revision(plugin_dir)
            synced[plugin_name] = (plugin_dir, revision, plugin_cfg.trusted)
            logger.info(
                "Plugin repo ready",
                plugin=plugin_name,
                ref=plugin_cfg.ref,
                sha=revision[:12],
                trusted=plugin_cfg.trusted,
                path=str(plugin_dir),
            )
        except Exception:
            logger.exception(
                "Failed to sync plugin repository",
                plugin=plugin_name,
                repo=plugin_cfg.repo,
                ref=plugin_cfg.ref,
            )
            raise

    return synced


# ---------------------------------------------------------------------------
# Shared install logic
# ---------------------------------------------------------------------------


def _is_plugin_importable(plugin_dir: Path) -> bool:
    """Check if a plugin package is actually importable in the current environment.

    Reads the pyproject.toml to find the package name, then tries to import it.
    """
    import tomllib

    pyproject = plugin_dir / "pyproject.toml"
    if not pyproject.exists():
        return False

    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        package_name = data.get("project", {}).get("name", "").replace("-", "_")
        if not package_name:
            return False

        # Try to import the package
        import importlib.util
        return importlib.util.find_spec(package_name) is not None
    except Exception:
        return False


def _install_if_needed(
    plugin_name: str,
    plugin_dir: Path,
    revision: str,
    install_state: dict[str, dict[str, str]],
    plugins_dir: Path,
    **log_extra: str,
) -> bool:
    """Install a plugin if its revision changed since last install.

    Updates *install_state* in-place and persists it on change.
    Returns True if the plugin was (re)installed, False if already up to date.
    """
    installed_revision = (install_state.get(plugin_name) or {}).get("revision")

    # Check both revision match AND that the plugin is actually importable
    if installed_revision == revision and _is_plugin_importable(plugin_dir):
        logger.info(
            "Plugin already up to date",
            plugin=plugin_name,
            revision=revision,
            **log_extra,
        )
        return False

    # If revision matches but plugin isn't importable, the venv was likely recreated
    if installed_revision == revision:
        logger.info(
            "Plugin revision matches but not importable (venv recreated?)",
            plugin=plugin_name,
            revision=revision,
            **log_extra,
        )

    if installed_revision:
        logger.info(
            "Plugin revision changed",
            plugin=plugin_name,
            from_revision=installed_revision,
            to_revision=revision,
            **log_extra,
        )
    else:
        logger.info(
            "Plugin revision discovered",
            plugin=plugin_name,
            revision=revision,
            **log_extra,
        )
    logger.info(
        "Installing plugin into host environment",
        plugin=plugin_name,
        revision=revision,
    )
    _install_plugin_in_host_env(plugin_name, plugin_dir)
    install_state[plugin_name] = {"revision": revision}
    _save_install_state(plugins_dir, install_state)
    return True


# ---------------------------------------------------------------------------
# Phase 2: Install verified plugins into host environment
# ---------------------------------------------------------------------------


def install_verified_plugins(verified: dict[str, tuple[Path, str]]) -> dict[str, Path]:
    """Install plugins that passed verification into the host Python environment.

    Args:
        verified: Dict of {name: (plugin_dir, git_sha)} — only verified plugins.

    Returns:
        Dict of {name: plugin_dir} for successfully installed plugins.
    """
    s = get_settings()
    install_state = _load_install_state(s.plugins_dir)
    installed: dict[str, Path] = {}

    for plugin_name, (plugin_dir, revision) in verified.items():
        try:
            _install_if_needed(plugin_name, plugin_dir, revision, install_state, s.plugins_dir)
            installed[plugin_name] = plugin_dir
        except Exception:
            logger.exception(
                "Failed to install verified plugin",
                plugin=plugin_name,
            )
            raise

    return installed


# ---------------------------------------------------------------------------
# Legacy combined API (kept for build command which doesn't need verification)
# ---------------------------------------------------------------------------


def sync_configured_plugins() -> dict[str, Path]:
    """Sync + install all configured plugins (no verification).

    Used by `pynchy build` and other contexts where verification is
    not needed (e.g., the user is building the container image).
    """
    s = get_settings()
    s.plugins_dir.mkdir(parents=True, exist_ok=True)
    install_state = _load_install_state(s.plugins_dir)
    synced: dict[str, Path] = {}

    for plugin_name, plugin_cfg in s.plugins.items():
        if not plugin_cfg.enabled:
            continue
        try:
            plugin_dir = _sync_single_plugin(plugin_name, plugin_cfg, s.plugins_dir)
            revision = _plugin_revision(plugin_dir)
            _install_if_needed(
                plugin_name,
                plugin_dir,
                revision,
                install_state,
                s.plugins_dir,
                ref=plugin_cfg.ref,
            )
            synced[plugin_name] = plugin_dir
            logger.info(
                "Plugin repo ready",
                plugin=plugin_name,
                ref=plugin_cfg.ref,
                path=str(plugin_dir),
            )
        except Exception:
            logger.exception(
                "Failed to sync plugin repository",
                plugin=plugin_name,
                repo=plugin_cfg.repo,
                ref=plugin_cfg.ref,
            )
            raise

    return synced
