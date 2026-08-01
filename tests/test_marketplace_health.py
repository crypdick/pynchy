"""Tests for the aggregate-only marketplace health projection."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.actions.api import ACTION_SPECS
from pynchy.config.api import McpTool, McpToolConfig, PluginConfig, tool_process_environment
from pynchy.host.orchestrator.plugin_configuration import configure_marketplace_health_plugin
from pynchy.plugins.api import HostActionAccess, validate_host_action_descriptors
from pynchy.plugins.integrations.marketplace_health import (
    MARKETPLACE_HEALTH_HOST_ACTIONS,
    MarketplaceHealthOptions,
    MarketplaceHealthPlugin,
    MarketplaceHealthRuntime,
    build_marketplace_health_snapshot,
    configure_marketplace_health_runtime,
)
from pynchy.plugins.integrations.proton_bridge import ProtonMailbox, ProtonMailboxList
from pynchy.plugins.integrations.proton_bridge_config import ProtonMailError
from pynchy.process_environment import filtered_process_environment

if TYPE_CHECKING:
    from pathlib import Path


def _write_state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "pending": {
                    "private-buyer-id-1": {"status": "pending", "body": "private body"},
                    "private-buyer-id-2": {"status": "awaiting_reply", "email": "private"},
                    "private-buyer-id-3": {"status": "closed"},
                    "private-buyer-id-4": {"status": "pending"},
                }
            }
        ),
        encoding="utf-8",
    )


def _settings(state_file: Path):
    return make_settings(
        plugins={
            "marketplace-health": PluginConfig(
                options={"pending_actions_file": str(state_file), "reader_tool": "proton-mail"}
            )
        },
        tools={
            "proton-mail": McpTool(
                type="mcp",
                optional_env=["PYNCHY_PROTON_BRIDGE_IMAP_PORT"],
                mcp=McpToolConfig(
                    runtime="script",
                    command="uv",
                    port=8475,
                    env={
                        "PYNCHY_PROTON_BRIDGE_USERNAME": "reader@example.test",
                        "PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND": "/usr/bin/read-secret",
                    },
                ),
            )
        },
    )


def _configure_runtime(settings) -> None:
    plugin = settings.plugins["marketplace-health"]

    def reader_environment(name: str) -> dict[str, str] | None:
        tool = settings.tools.get(name)
        if not isinstance(tool, McpTool):
            return None
        return filtered_process_environment({**tool.mcp.env, **tool_process_environment(tool)})

    configure_marketplace_health_runtime(
        MarketplaceHealthRuntime(
            options=MarketplaceHealthOptions.model_validate(plugin.options),
            reader_environment=reader_environment,
        )
    )


@pytest.mark.action("marketplace.health.read")
def test_snapshot_returns_only_counts_and_reader_health(tmp_path: Path, monkeypatch) -> None:
    state_file = tmp_path / "pending_actions.json"
    _write_state(state_file)
    client = MagicMock()
    client.list_mailboxes.return_value = ProtonMailboxList(
        mailboxes=[ProtonMailbox(name="Private Mailbox", mailbox="PRIVATE")]
    )
    create_client = MagicMock(return_value=client)
    monkeypatch.setenv("PYNCHY_PROTON_BRIDGE_IMAP_PORT", "2143")
    monkeypatch.setenv("UNRELATED_HOST_TOKEN", "must-not-leak")

    _configure_runtime(_settings(state_file))
    with (
        patch(
            "pynchy.plugins.integrations.marketplace_health.create_proton_mail_client",
            create_client,
        ),
    ):
        snapshot = build_marketplace_health_snapshot(
            MarketplaceHealthOptions(pending_actions_file=state_file)
        ).model_dump(mode="json")

    assert snapshot == {
        "counts": {"pending": 2, "awaiting_reply": 1},
        "reader_health": {"status": "ready", "reason": "ready"},
    }
    assert "private" not in json.dumps(snapshot).casefold()
    environment = create_client.call_args.kwargs["environment"]
    assert environment["PYNCHY_PROTON_BRIDGE_USERNAME"] == "reader@example.test"
    assert environment["PYNCHY_PROTON_BRIDGE_IMAP_PORT"] == "2143"
    assert "UNRELATED_HOST_TOKEN" not in environment
    client.list_mailboxes.assert_called_once_with()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("Configure the Proton Bridge reader first", "reader_not_configured"),
        ("Could not retrieve the Proton Bridge app password", "reader_credentials_unavailable"),
        ("Proton Bridge IMAP request failed", "reader_connection_unavailable"),
        ("unexpected reader error", "reader_unavailable"),
    ],
)
def test_snapshot_sanitizes_reader_failures(
    tmp_path: Path,
    error: str,
    reason: str,
) -> None:
    state_file = tmp_path / "pending_actions.json"
    _write_state(state_file)
    client = MagicMock()
    client.list_mailboxes.side_effect = ProtonMailError(error)

    _configure_runtime(_settings(state_file))
    with (
        patch(
            "pynchy.plugins.integrations.marketplace_health.create_proton_mail_client",
            return_value=client,
        ),
    ):
        snapshot = build_marketplace_health_snapshot(
            MarketplaceHealthOptions(pending_actions_file=state_file)
        )

    assert snapshot.reader_health.model_dump() == {"status": "unavailable", "reason": reason}


def test_snapshot_rejects_invalid_state_without_echoing_content(tmp_path: Path) -> None:
    state_file = tmp_path / "pending_actions.json"
    state_file.write_text('{"pending": ["private body"]}', encoding="utf-8")

    with pytest.raises(TypeError, match="invalid structure") as exc_info:
        build_marketplace_health_snapshot(MarketplaceHealthOptions(pending_actions_file=state_file))

    assert "private body" not in str(exc_info.value)


def test_snapshot_rejects_invalid_state_entries(tmp_path: Path) -> None:
    state_file = tmp_path / "pending_actions.json"
    state_file.write_text(json.dumps({"pending": {"buyer": "not an entry"}}), encoding="utf-8")

    with pytest.raises(TypeError, match="invalid entry"):
        build_marketplace_health_snapshot(MarketplaceHealthOptions(pending_actions_file=state_file))


def test_snapshot_rejects_invalid_state_status(tmp_path: Path) -> None:
    state_file = tmp_path / "pending_actions.json"
    state_file.write_text(json.dumps({"pending": {"buyer": {"status": 1}}}), encoding="utf-8")

    with pytest.raises(TypeError, match="invalid status"):
        build_marketplace_health_snapshot(MarketplaceHealthOptions(pending_actions_file=state_file))


def test_snapshot_rejects_missing_state_status(tmp_path: Path) -> None:
    state_file = tmp_path / "pending_actions.json"
    state_file.write_text(json.dumps({"pending": {"buyer": {}}}), encoding="utf-8")

    with pytest.raises(TypeError, match="invalid status"):
        build_marketplace_health_snapshot(MarketplaceHealthOptions(pending_actions_file=state_file))


def test_snapshot_rejects_missing_or_oversized_state_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="state is unavailable"):
        build_marketplace_health_snapshot(MarketplaceHealthOptions(pending_actions_file=missing))

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * 1_000_001)
    with pytest.raises(ValueError, match="exceeds the safe read limit"):
        build_marketplace_health_snapshot(MarketplaceHealthOptions(pending_actions_file=oversized))


def test_snapshot_reports_unconfigured_reader(tmp_path: Path) -> None:
    state_file = tmp_path / "pending_actions.json"
    _write_state(state_file)
    configure_marketplace_health_runtime(
        MarketplaceHealthRuntime(
            options=MarketplaceHealthOptions(pending_actions_file=state_file),
            reader_environment=lambda _tool: None,
        )
    )

    snapshot = build_marketplace_health_snapshot(
        MarketplaceHealthOptions(pending_actions_file=state_file)
    )

    assert snapshot.reader_health.model_dump() == {
        "status": "unavailable",
        "reason": "reader_not_configured",
    }


@pytest.mark.action("marketplace.health.read")
async def test_marketplace_service_returns_configured_snapshot(tmp_path: Path) -> None:
    state_file = tmp_path / "pending_actions.json"
    _write_state(state_file)
    configure_marketplace_health_runtime(
        MarketplaceHealthRuntime(
            options=MarketplaceHealthOptions(pending_actions_file=state_file),
            reader_environment=lambda _tool: None,
        )
    )

    action = MARKETPLACE_HEALTH_HOST_ACTIONS.actions[0]

    assert await action.handler({}) == {
        "result": {
            "counts": {"pending": 2, "awaiting_reply": 1},
            "reader_health": {"status": "unavailable", "reason": "reader_not_configured"},
        }
    }


def test_marketplace_state_file_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        MarketplaceHealthOptions(pending_actions_file="relative.json")


def test_host_action_is_a_read_only_validated_action() -> None:
    action = MARKETPLACE_HEALTH_HOST_ACTIONS.actions[0]

    assert action.access is HostActionAccess.READ
    assert (
        validate_host_action_descriptors(MARKETPLACE_HEALTH_HOST_ACTIONS.actions, ACTION_SPECS)
        == ()
    )


async def test_empty_options_leave_plugin_action_unconfigured() -> None:
    plugin_manager = MagicMock()
    plugin_manager.get_plugin.return_value = MarketplaceHealthPlugin()

    configure_marketplace_health_plugin(
        plugin_manager,
        make_settings(plugins={"marketplace-health": PluginConfig()}),
    )

    assert await MARKETPLACE_HEALTH_HOST_ACTIONS.actions[0].handler({}) == {
        "error": "Marketplace health projection is not configured"
    }
