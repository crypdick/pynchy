"""Public integration behavior before host composition configures runtimes."""

from __future__ import annotations

import pytest

from pynchy.plugins.integrations import gog
from pynchy.plugins.integrations.caldav import CalDAVMcpServerPlugin
from pynchy.plugins.integrations.linear_boot import linear_workspace_enabled
from pynchy.plugins.integrations.linear_webhooks import linear_webhook_routes
from pynchy.plugins.integrations.marketplace_health import MarketplaceHealthPlugin
from pynchy.workspace.api import WorkspaceProfile


@pytest.mark.asyncio
async def test_caldav_service_rejects_requests_before_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.caldav._runtime", None)
    action = CalDAVMcpServerPlugin().pynchy_service_handler().action_for("list_calendars")
    assert action is not None

    assert await action.handler({}) == {"error": "CalDAV runtime has not been configured"}


@pytest.mark.asyncio
async def test_gog_service_rejects_requests_before_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.gog._config._runtime", None)
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_search")
    assert action is not None

    with pytest.raises(RuntimeError, match="Gog runtime has not been configured"):
        await action.handler({"source_group": "workspace"})


def test_linear_workspace_lookup_rejects_requests_before_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.linear_boot._runtime", None)

    with pytest.raises(RuntimeError, match="Linear boot runtime has not been configured"):
        linear_workspace_enabled(
            WorkspaceProfile(
                jid="slack:workspace",
                folder="workspace",
                name="Workspace",
                trigger="@Pynchy",
            )
        )


def test_linear_webhook_routes_reject_runtime_before_configuration(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.linear_webhooks._runtime", None)

    with pytest.raises(RuntimeError, match="Linear webhook runtime has not been configured"):
        linear_webhook_routes()


@pytest.mark.asyncio
async def test_marketplace_service_rejects_requests_before_runtime_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.marketplace_health._runtime", None)
    action = (
        MarketplaceHealthPlugin().pynchy_service_handler().action_for("marketplace_health_snapshot")
    )
    assert action is not None

    assert await action.handler({}) == {
        "error": "Marketplace health runtime has not been configured"
    }
