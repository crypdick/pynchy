"""Behavioral coverage for plugin-owned native scheduled jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

from conftest import make_settings

from pynchy.config.jobs import JobConfig
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator.job_sources import configure_plugin_jobs

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class _Hooks:
    pynchy_job_specs: Callable[[], list[tuple[dict[str, object], ...]]]


@dataclass(frozen=True)
class _PluginManager:
    hook: _Hooks


def test_plugin_jobs_join_native_agent_and_deterministic_paths() -> None:
    settings = make_settings(
        profiles={"fam": ProfileConfig()},
        workspaces={"fam": WorkspaceConfig(profiles=["fam"])},
    )
    specs = (
        {
            "name": "vault-agent",
            "config": {
                "schedule": "0 8 * * *",
                "workspace": "fam",
                "prompt": "Review fam.",
                "pre_run_command": "scripts/gate.py",
            },
        },
        {
            "name": "vault-shell",
            "config": {
                "interval_minutes": 360,
                "workspace": "fam",
                "agent": False,
                "command": "scripts/remind.py",
            },
        },
    )
    plugin_manager = _PluginManager(hook=_Hooks(pynchy_job_specs=lambda: [specs]))

    with patch(
        "pynchy.host.orchestrator.job_sources.get_settings",
        return_value=settings,
    ):
        configure_plugin_jobs(plugin_manager)

    assert settings.jobs["vault-agent"].pre_run_command == "scripts/gate.py"
    assert settings.jobs["vault-shell"].is_deterministic is True
    assert settings.jobs["vault-shell"].interval_minutes == 360
    assert "vault-shell" not in settings.cron_jobs


def test_user_job_wins_over_plugin_registry_entry() -> None:
    user_job = JobConfig(
        schedule="0 9 * * *",
        workspace="fam",
        prompt="User-owned prompt.",
    )
    settings = make_settings(
        profiles={"fam": ProfileConfig()},
        workspaces={"fam": WorkspaceConfig(profiles=["fam"])},
        jobs={"same-name": user_job},
    )
    plugin_manager = _PluginManager(
        hook=_Hooks(
            pynchy_job_specs=lambda: [
                (
                    {
                        "name": "same-name",
                        "config": {
                            "schedule": "0 8 * * *",
                            "workspace": "fam",
                            "prompt": "Plugin prompt.",
                        },
                    },
                )
            ]
        )
    )

    with patch(
        "pynchy.host.orchestrator.job_sources.get_settings",
        return_value=settings,
    ):
        configure_plugin_jobs(plugin_manager)

    assert settings.jobs["same-name"].prompt == "User-owned prompt."


def test_user_replacement_survives_plugin_reconfiguration() -> None:
    settings = make_settings(
        profiles={"fam": ProfileConfig()},
        workspaces={"fam": WorkspaceConfig(profiles=["fam"])},
    )
    plugin_manager = _PluginManager(
        hook=_Hooks(
            pynchy_job_specs=lambda: [
                (
                    {
                        "name": "same-name",
                        "config": {
                            "schedule": "0 8 * * *",
                            "workspace": "fam",
                            "prompt": "Plugin prompt.",
                        },
                    },
                )
            ]
        )
    )
    user_job = JobConfig(
        schedule="0 9 * * *",
        workspace="fam",
        prompt="User-owned prompt.",
    )

    with patch(
        "pynchy.host.orchestrator.job_sources.get_settings",
        return_value=settings,
    ):
        configure_plugin_jobs(plugin_manager)
        settings.jobs["same-name"] = user_job
        configure_plugin_jobs(plugin_manager)

    assert settings.jobs["same-name"] == user_job
