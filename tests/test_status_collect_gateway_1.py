"""Tests for the operational status collector and /status endpoint.

All subsystem behaviour is exercised through the public ``collect_status()``
entry point (and the ``/status`` HTTP endpoint), asserting on the observable
status dict rather than importing the private per-section collectors.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp.test_utils import AioHTTPTestCase

from pynchy.canaries.api import declared_canary_scenarios
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.status import collect_status, record_start_time
from pynchy.state import (
    begin_webhook_effect,
    init_test_database,
    mark_webhook_effect_executing,
    mark_webhook_effect_outcome_unknown,
)
from pynchy.webhook_effects import WebhookEffectScope
from tests.status_support import (
    MockHttpDeps,
    MockStatusDeps,
    _inert_status,
    _runtime,
)

if TYPE_CHECKING:
    from aiohttp import web

_S = "pynchy.host.orchestrator.status"

_EMPTY_STATS = {
    "total_inbound": 0,
    "total_outbound": 0,
    "last_received_at": None,
    "last_sent_at": None,
    "pending_deliveries": 0,
}


class TestCollectGateway:
    @pytest.mark.asyncio
    async def test_non_litellm_mode(self):
        deps = MockStatusDeps(gateway={"mode": "builtin", "redaction": "enforced"})
        with _inert_status():
            result = await collect_status(deps, time.monotonic())
        assert result["gateway"] == {"mode": "builtin", "redaction": "enforced"}

    @pytest.mark.asyncio
    async def test_litellm_container_status(self):
        deps = MockStatusDeps(gateway={"mode": "litellm", "port": 4000, "key": "sk-test"})

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {
            "status": "healthy",
            "db": "connected",
            "litellm_version": "1.2.3",
        }
        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None

        deps.get_container_state.side_effect = ["running", "running"]
        with _inert_status(), patch("aiohttp.ClientSession", return_value=mock_session):
            result = await collect_status(deps, time.monotonic())

        gateway = result["gateway"]
        assert gateway["litellm_container"] == "running"
        assert gateway["postgres_container"] == "running"
        assert gateway["ready"] is True
        assert gateway["database"] == "connected"
        assert gateway["litellm_version"] == "1.2.3"
        mock_session.get.assert_called_once_with(
            "http://localhost:4000/health/readiness",
            headers={"Authorization": "Bearer sk-test"},
            timeout=mock_session.get.call_args.kwargs["timeout"],
        )

    @pytest.mark.asyncio
    async def test_external_litellm_reports_readiness_without_fake_container_states(self):
        deps = MockStatusDeps(
            gateway={"mode": "litellm", "managed": False, "port": 4000, "key": "sk-test"}
        )
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"status": "healthy", "db": "connected"}
        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None

        with _inert_status(), patch("aiohttp.ClientSession", return_value=mock_session):
            result = await collect_status(deps, time.monotonic())

        gateway = result["gateway"]
        assert gateway["ready"] is True
        assert "litellm_container" not in gateway
        assert "postgres_container" not in gateway
        deps.get_container_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_litellm_container_status_uses_runtime_namespace(self, monkeypatch):
        monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy-feature-test")
        deps = MockStatusDeps(gateway={"mode": "litellm"})

        deps.get_container_state.return_value = "running"
        with _inert_status():
            await collect_status(deps, time.monotonic())

        inspected = [call.args[0] for call in deps.get_container_state.await_args_list]
        assert inspected == [
            "pynchy-feature-test-litellm",
            "pynchy-feature-test-litellm-db",
        ]

    @pytest.mark.asyncio
    async def test_litellm_readiness_accepts_current_healthy_shape(self):
        deps = MockStatusDeps(gateway={"mode": "litellm", "port": 4000, "key": "sk-test"})

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"status": "healthy", "db": "connected"}
        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None

        deps.get_container_state.side_effect = ["running", "running"]
        with _inert_status(), patch("aiohttp.ClientSession", return_value=mock_session):
            result = await collect_status(deps, time.monotonic())

        assert result["gateway"]["ready"] is True
        assert result["gateway"]["database"] == "connected"

    @pytest.mark.asyncio
    async def test_gateway_health_failure_returns_none(self):
        """When gateway HTTP check fails, model counts are None."""
        deps = MockStatusDeps(gateway={"mode": "litellm", "port": 4000, "key": "sk-test"})
        deps.get_container_state.return_value = "running"
        with _inert_status():  # aiohttp.ClientSession stays inert (raises) → health check fails.
            result = await collect_status(deps, time.monotonic())

        gateway = result["gateway"]
        assert gateway["litellm_container"] == "running"
        assert gateway["ready"] is None

    @pytest.mark.asyncio
    async def test_missing_port_skips_health_check(self):
        """When port or key is missing, health check is skipped."""
        deps = MockStatusDeps(gateway={"mode": "litellm"})
        deps.get_container_state.side_effect = ["running", "stopped"]
        with _inert_status():
            result = await collect_status(deps, time.monotonic())

        gateway = result["gateway"]
        assert gateway["litellm_container"] == "running"
        assert gateway["postgres_container"] == "stopped"
        assert "healthy_models" not in gateway


class TestContainerState:
    """Docker container state resolution, observed via gateway container fields."""

    @staticmethod
    def _litellm_deps() -> MockStatusDeps:
        return MockStatusDeps(gateway={"mode": "litellm", "port": 4000, "key": "sk-test"})

    @pytest.mark.asyncio
    async def test_running_container(self):
        deps = self._litellm_deps()
        deps.get_container_state.return_value = "running"
        with _inert_status():
            result = await collect_status(deps, time.monotonic())
        assert result["gateway"]["litellm_container"] == "running"

    @pytest.mark.asyncio
    async def test_stopped_container(self):
        deps = self._litellm_deps()
        deps.get_container_state.return_value = "exited"
        with _inert_status():
            result = await collect_status(deps, time.monotonic())
        assert result["gateway"]["litellm_container"] == "exited"

    @pytest.mark.asyncio
    async def test_not_found(self):
        deps = self._litellm_deps()
        with _inert_status():
            result = await collect_status(deps, time.monotonic())
        assert result["gateway"]["litellm_container"] == "not_found"


class TestCollectStatus:
    @pytest.mark.asyncio
    async def test_returns_all_sections(self):
        """Top-level collect_status assembles all subsystem sections."""
        deps = MockStatusDeps(
            channels={"whatsapp": True, "slack": False},
            workspace_count=5,
            active_sessions=2,
        )
        record_start_time()
        deps.git_status.get_head_sha.return_value = "abc123"
        deps.git_status.get_head_commit_message.return_value = "test"

        with (
            _inert_status(),
            patch(
                f"{_S}.get_messaging_stats",
                new_callable=AsyncMock,
                return_value={
                    "total_inbound": 100,
                    "total_outbound": 50,
                    "last_received_at": None,
                    "last_sent_at": None,
                    "pending_deliveries": 0,
                },
            ),
        ):
            result = await collect_status(deps, time.monotonic() - 120)

        expected_keys = {
            "service",
            "deploy",
            "channels",
            "connections",
            "gateway",
            "queue",
            "repos",
            "messages",
            "tasks",
            "host_jobs",
            "temporal",
            "canaries",
            "capabilities",
            "speech",
            "groups",
        }
        assert set(result.keys()) == expected_keys

        # In-memory sections are passed through from deps
        assert result["channels"] == {"whatsapp": True, "slack": False}
        assert result["groups"]["total"] == 5
        assert result["groups"]["active_sessions"] == 2
        assert result["service"]["status"] == "ok"
        assert result["service"]["uptime_seconds"] >= 120
        assert result["messages"]["total_inbound"] == 100
        assert result["canaries"] == {"summary": {"unresolved_regressions": 0}}
        assert result["capabilities"] == {"summary": {}, "workspaces": []}


class TestStatusEndpoint(AioHTTPTestCase):
    """Tests for GET /status endpoint."""

    async def get_application(self) -> web.Application:
        self.mock_deps = MockStatusDeps(
            channels={"whatsapp": True},
            workspace_count=3,
            active_sessions=1,
        )
        self.http_deps = MockHttpDeps()
        return create_http_app(self.http_deps, runtime=_runtime(), status_deps=self.mock_deps)

    async def test_status_returns_200(self):
        """GET /status returns 200 with structured JSON."""
        record_start_time()

        with _inert_status():
            resp = await self.client.get("/status")
            assert resp.status == 200
            data = await resp.json()
            assert "service" in data
            assert "deploy" in data
            assert "channels" in data
            assert "gateway" in data
            assert "queue" in data
            assert "groups" in data
            assert data["channels"] == {"whatsapp": True}

    async def test_work_items_endpoint_returns_empty_bounded_projection(self):
        await init_test_database()

        response = await self.client.get("/work-items?workspace=project&limit=1")
        invalid = await self.client.get("/work-items?limit=zero")

        assert response.status == 200
        assert await response.json() == {"workspace": "project", "work_items": []}
        assert invalid.status == 400

    async def test_actions_endpoint_returns_empty_bounded_projection(self):
        await init_test_database()

        response = await self.client.get("/actions?workspace=project&limit=1")
        invalid = await self.client.get("/actions?limit=zero")

        assert response.status == 200
        assert await response.json() == {"workspace": "project", "actions": []}
        assert invalid.status == 400

    async def test_webhook_effect_reconciliation_requires_explicit_absence_proof(self):
        await init_test_database()
        effect_id = await begin_webhook_effect(
            WebhookEffectScope(
                provider="linear",
                account="project",
                event_type="Comment",
                event_action="create",
                subject_id="issue-1",
                intent_fingerprint="intent-fingerprint",
            )
        )
        await mark_webhook_effect_executing(effect_id)
        await mark_webhook_effect_outcome_unknown(effect_id)

        listed = await self.client.get("/webhook-effects")
        rejected = await self.client.post(
            f"/webhook-effects/{effect_id}/reconcile-absent",
            json={"verified_absent": False},
        )
        reconciled = await self.client.post(
            f"/webhook-effects/{effect_id}/reconcile-absent",
            json={"verified_absent": True},
        )
        after = await self.client.get("/webhook-effects")

        assert listed.status == 200
        listed_body = await listed.json()
        assert listed_body["effects"][0]["intent_fingerprint"] == "intent-fingerprint"
        assert rejected.status == 400
        assert reconciled.status == 200
        assert await reconciled.json() == {
            "status": "reconciled_absent",
            "released_deliveries": 0,
        }
        assert await after.json() == {"status": "outcome_unknown", "effects": []}

    async def test_canary_report_and_history_endpoints(self):
        await init_test_database()

        report_response = await self.client.get("/canaries/report")
        report = await report_response.json()
        history_response = await self.client.get("/canaries/runs?limit=1")
        history = await history_response.json()
        invalid_response = await self.client.get("/canaries/runs?limit=zero")

        assert report_response.status == 200
        assert report["summary"]["declared_scenarios"] == len(declared_canary_scenarios())
        assert history_response.status == 200
        assert history == {"runs": []}
        assert invalid_response.status == 400

    async def test_capabilities_endpoint_returns_all_or_one_workspace(self):
        all_payload = {"summary": {"ready": 1}, "workspaces": []}
        workspace_payload = {
            "workspace": "matrix",
            "summary": {"ready": 1},
            "capabilities": [],
        }
        snapshot = Mock()
        snapshot.to_dict.return_value = workspace_payload
        self.http_deps.get_canary_report = AsyncMock(return_value={"scenarios": []})

        with (
            patch(
                "pynchy.host.orchestrator.http_server.collect_capability_status",
                new_callable=AsyncMock,
                return_value=all_payload,
            ),
            patch(
                "pynchy.host.orchestrator.http_server.resolve_workspace_capabilities",
                new_callable=AsyncMock,
                return_value=snapshot,
            ),
        ):
            all_response = await self.client.get("/capabilities")
            workspace_response = await self.client.get("/capabilities?workspace=matrix")

        assert all_response.status == 200
        assert await all_response.json() == all_payload
        assert workspace_response.status == 200
        assert await workspace_response.json() == workspace_payload
