"""Public Gog plugin contracts for workspace fencing and receipts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.plugins.api import CapabilityProbeContext, ProbeStatus
from pynchy.plugins.integrations import gog


def _configure(*, account: str | None = "you@example.com") -> None:
    gog.configure_gog_runtime(
        gog.GogRuntime(
            config=gog.GogConfig(account=account),
            home=Path.cwd() / "pynchy-gog-test",
            oauth_client_path=None,
            workspace_enables_gog=lambda workspace: workspace == "workspace",
        )
    )


@pytest.mark.asyncio
async def test_gog_action_rejects_requests_without_a_workspace_context() -> None:
    _configure()
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_search")
    assert action is not None

    result = await action.handler({"query": "from:friend@example.com"})

    assert result == {"error": "gog_gmail_search is not enabled for this workspace"}


@pytest.mark.asyncio
async def test_gog_probe_reports_missing_account_configuration() -> None:
    _configure(account=None)
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_search")
    assert action is not None
    assert action.capability.probe is not None

    result = await action.capability.probe(CapabilityProbeContext("workspace"))

    assert result.status is ProbeStatus.UNAVAILABLE
    assert "configure" in result.reason.lower()


@pytest.mark.asyncio
async def test_gog_probe_reports_invalid_runtime_configuration() -> None:
    class InvalidRuntime:
        @property
        def config(self):
            raise ValueError("invalid Gog options")

    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_search")
    assert action is not None
    assert action.capability.probe is not None

    with patch(
        "pynchy.plugins.integrations.gog._plugin.gog_runtime",
        return_value=InvalidRuntime(),
    ):
        result = await action.capability.probe(CapabilityProbeContext("workspace"))

    assert result.status is ProbeStatus.UNAVAILABLE
    assert "invalid" in result.reason.lower()


def test_gog_receipt_rejects_missing_serialized_provider_result() -> None:
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_send")
    assert action is not None
    assert action.action_intent is not None

    with pytest.raises(TypeError, match="omitted"):
        action.action_intent.receipt_from_response({})


def test_gog_receipt_rejects_non_object_provider_result() -> None:
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_send")
    assert action is not None
    assert action.action_intent is not None

    with pytest.raises(TypeError, match="object or array"):
        action.action_intent.receipt_from_response({"result": json.dumps("scalar")})


def test_gog_receipt_uses_nested_provider_identifier() -> None:
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_send")
    assert action is not None
    assert action.action_intent is not None

    receipt = action.action_intent.receipt_from_response(
        {"result": json.dumps({"result": {"id": "nested-message"}})}
    )

    assert receipt.provider_request_id == "nested-message"
