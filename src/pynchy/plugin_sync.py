"""Sync plugin repositories declared in config.toml."""

from __future__ import annotations

import re
import subprocess
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


def sync_configured_plugins() -> dict[str, Path]:
    """Ensure configured plugins are cloned and up-to-date at startup."""
    s = get_settings()
    s.plugins_dir.mkdir(parents=True, exist_ok=True)
    synced: dict[str, Path] = {}

    for plugin_name, plugin_cfg in s.plugins.items():
        if not plugin_cfg.enabled:
            continue
        try:
            plugin_dir = _sync_single_plugin(plugin_name, plugin_cfg, s.plugins_dir)
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
