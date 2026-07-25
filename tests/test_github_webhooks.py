"""Behavioral coverage for GitHub PR webhook notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from functools import partial
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_settings

from pynchy.config import PluginConfig
from pynchy.host.orchestrator.http_control import (
    ControlPlaneRuntime,
    ControlPlaneToken,
    RequestRateLimiter,
)
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations.github import GitHubWebhookPlugin
from pynchy.plugins.integrations.github_webhooks import (
    GITHUB_MAX_WEBHOOK_BODY_BYTES,
    GitHubWebhookRouteConfig,
    github_webhook_routes,
    parse_github_webhook,
)
from pynchy.plugins.webhooks import WebhookAuthenticationError, WebhookPayloadError, WebhookRoute
from pynchy.state import get_all_tasks, get_webhook_receipt, init_test_database
from pynchy.types import ScheduledTask, WorkspaceProfile

_SIGNING_KEY = "github-webhook-test-signing-key-long-enough"
_DELIVERY_ID = "ee7b4ec5-daa1-48fa-8c8f-c4de20e9d65f"
_REPOSITORY = "example/project"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _payload(
    *,
    action: str = "edited",
    changes: dict[str, object] | None = None,
    repository: str = _REPOSITORY,
) -> dict[str, object]:
    return {
        "action": action,
        "number": 42,
        "repository": {"full_name": repository},
        "pull_request": {"html_url": f"https://github.com/{repository}/pull/42"},
        "changes": changes or {"body": {"from": "previous description"}},
    }


def _signed_request(payload: dict[str, object], event_type: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(_SIGNING_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
    return raw_body, {
        "Content-Type": "application/json",
        "X-GitHub-Delivery": _DELIVERY_ID,
        "X-GitHub-Event": event_type,
        "X-Hub-Signature-256": f"sha256={signature}",
    }


def _config() -> GitHubWebhookRouteConfig:
    return GitHubWebhookRouteConfig(name="project", workspace="project", repository=_REPOSITORY)


def _route() -> WebhookRoute:
    config = _config()
    return WebhookRoute(
        provider="github",
        name=config.name,
        workspace=config.workspace,
        secret_env=config.secret_env,
        parse=partial(parse_github_webhook, config=config),
        max_body_bytes=config.max_body_bytes,
        rate_limit_requests=config.rate_limit_requests,
        rate_limit_window_seconds=config.rate_limit_window_seconds,
    )


def test_config_uses_github_documented_payload_maximum() -> None:
    assert _config().max_body_bytes == GITHUB_MAX_WEBHOOK_BODY_BYTES


def test_description_change_maps_to_literal_host_notification() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(), "pull_request")

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is None
    assert event.external_context is None
    assert event.host_message == (
        "GitHub PR update — example/project#42: PR description updated.\n"
        "https://github.com/example/project/pull/42"
    )


def test_merged_pull_request_starts_an_agent_follow_up_turn() -> None:
    now = datetime.now(UTC)
    payload = _payload(action="closed", changes={})
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["merged"] = True
    raw_body, headers = _signed_request(payload, "pull_request")

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.action == "merged"
    assert event.host_message is None
    assert "linear_find_issues_by_attachment_url" in (event.instructions or "")
    assert event.external_context == {
        "repository": "example/project",
        "pull_request_number": 42,
        "pull_request_url": "https://github.com/example/project/pull/42",
        "event": "merged",
    }


def test_ci_failure_maps_to_its_pull_request_notification() -> None:
    now = datetime.now(UTC)
    payload = _payload(action="completed", changes={})
    payload["check_run"] = {
        "name": "test",
        "conclusion": "failure",
        "pull_requests": [{"number": 42}],
    }
    raw_body, headers = _signed_request(payload, "check_run")

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.host_message == (
        "GitHub CI failure — example/project #42: test (failure).\n"
        "https://github.com/example/project/pull/42"
    )


def test_explicit_nonmergeable_pr_state_reports_a_merge_conflict() -> None:
    now = datetime.now(UTC)
    payload = _payload(action="synchronize", changes={})
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["mergeable_state"] = "dirty"
    raw_body, headers = _signed_request(payload, "pull_request")

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.host_message == (
        "GitHub PR update — example/project#42: merge conflict detected.\n"
        "https://github.com/example/project/pull/42"
    )


def test_rejects_bad_signatures_and_repositories_before_notification() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(), "pull_request")

    with pytest.raises(WebhookAuthenticationError, match="signature"):
        parse_github_webhook(
            raw_body,
            {**headers, "X-Hub-Signature-256": "sha256=bad"},
            _SIGNING_KEY,
            now,
            config=_config(),
        )

    wrong_raw_body, wrong_headers = _signed_request(
        _payload(repository="unmapped/project"), "pull_request"
    )
    with pytest.raises(WebhookPayloadError, match="repository"):
        parse_github_webhook(wrong_raw_body, wrong_headers, _SIGNING_KEY, now, config=_config())


def test_plugin_routes_bind_each_repository_to_its_workspace() -> None:
    settings = make_settings(
        plugins={
            "github": PluginConfig(
                options={
                    "webhook_routes": [
                        {"name": "project", "workspace": "project", "repository": _REPOSITORY}
                    ]
                }
            )
        }
    )

    with patch("pynchy.plugins.integrations.github_webhooks.get_settings", return_value=settings):
        routes = github_webhook_routes()

    assert [(route.path, route.workspace) for route in routes] == [
        ("/webhooks/github/project", "project")
    ]


class _WebhookDeps:
    def __init__(self) -> None:
        self.workspace = WorkspaceProfile(
            jid="discord:project-channel",
            name="Project",
            folder="project",
            trigger="@Pynchy",
        )
        self.dispatched: list[ScheduledTask] = []
        self.host_messages: list[tuple[str, str]] = []

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    def admin_chat_jid(self) -> str:
        return "discord:pynchy-dev"

    def get_plugin_manager(self) -> object:
        return object()

    def get_workspace(self, folder: str) -> WorkspaceProfile | None:
        return self.workspace if folder == self.workspace.folder else None

    def dispatch_scheduled_task(self, task: ScheduledTask) -> None:
        self.dispatched.append(task)


def _public_runtime() -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        bind_host="0.0.0.0",  # noqa: S104, RUF100 - exercise public-bind auth policy
        port=8484,
        unix_socket=None,
        public_bind=True,
        remote_auth_required=True,
        allow_remote_deploy=False,
        auth_token=ControlPlaneToken("control-plane-token-that-is-long-enough"),
        rate_limiter=RequestRateLimiter(request_limit=100, window_seconds=60),
    )


async def test_delivery_notifies_its_route_workspace_without_agent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        raw_body, headers = _signed_request(_payload(), "pull_request")
        first_response = await client.post(
            "/webhooks/github/project", data=raw_body, headers=headers
        )
        first = await first_response.json()
        second_response = await client.post(
            "/webhooks/github/project", data=raw_body, headers=headers
        )
        second = await second_response.json()
    finally:
        await client.close()

    assert first_response.status == second_response.status == 200
    assert first == {"status": "notified", "duplicate": False}
    assert second == {"status": "notified", "duplicate": True}
    assert deps.host_messages == [
        (
            "discord:project-channel",
            (
                "GitHub PR update — example/project#42: PR description updated.\n"
                "https://github.com/example/project/pull/42"
            ),
        )
    ]
    assert not deps.dispatched
    assert not await get_all_tasks()
    receipt = await get_webhook_receipt("github", "project", _DELIVERY_ID)
    assert receipt is not None
    assert receipt.disposition == "notified"


async def test_merged_delivery_dispatches_one_agent_follow_up_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = _payload(action="closed", changes={})
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["merged"] = True
    try:
        raw_body, headers = _signed_request(payload, "pull_request")
        response = await client.post(
            "/webhooks/github/project",
            data=raw_body,
            headers=headers,
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body == {"status": "accepted", "duplicate": False}
    assert len(deps.dispatched) == 1
    task = deps.dispatched[0]
    assert task.group_folder == "project"
    assert "linear_find_issues_by_attachment_url" in task.prompt
    assert "https://github.com/example/project/pull/42" in task.prompt
    assert not deps.host_messages


def test_builtin_plugin_is_registered() -> None:
    with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
        plugin = get_plugin_manager().get_plugin("builtin-github")

    assert isinstance(plugin, GitHubWebhookPlugin)
