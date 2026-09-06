"""Behavioral coverage for GitHub PR webhook notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.conversation.models import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.host.orchestrator.http_control import (
    ControlPlaneRuntime,
    ControlPlaneToken,
    RequestRateLimiter,
)
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.plugins.api import (
    WebhookAuthenticationError,
    WebhookEvent,
    WebhookPayloadError,
    WebhookProcessingError,
    WebhookRoute,
)
from pynchy.plugins.integrations.github_webhook_models import GitHubPluginOptions
from pynchy.plugins.integrations.github_webhooks import (
    GITHUB_MAX_WEBHOOK_BODY_BYTES,
    GitHubWebhookRouteConfig,
    github_webhook_routes,
    parse_github_webhook,
    prepare_github_webhook_event,
)
from pynchy.plugins.integrations.linear_errors import LinearError
from pynchy.plugins.integrations.linear_work_item_provider import LinearWorkspaceIssueError
from pynchy.state import get_webhook_receipt, init_test_database
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pynchy.scheduling.api import ScheduledTask

_SIGNING_KEY = "github-webhook-test-signing-key-long-enough"
_DELIVERY_ID = "ee7b4ec5-daa1-48fa-8c8f-c4de20e9d65f"
_REPOSITORY = "example/project"


@dataclass(frozen=True)
class _Conversation:
    subject: ConversationSubject
    workspace: str = "project"


@dataclass(frozen=True)
class _Control:
    thread_jid: str = "discord:channel:linear-issue"
    closed: bool = False


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _payload(
    *,
    action: str = "edited",
    changes: dict[str, object] | None = None,
    repository: str = _REPOSITORY,
    sender: str = "repo-owner",
) -> dict[str, object]:
    return {
        "action": action,
        "number": 42,
        "repository": {"full_name": repository},
        "sender": {"login": sender},
        "pull_request": {
            "number": 42,
            "html_url": f"https://github.com/{repository}/pull/42",
        },
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
    return GitHubWebhookRouteConfig(name="project", repository=_REPOSITORY)


def _route() -> WebhookRoute:
    config = _config()
    return WebhookRoute(
        provider="github",
        name=config.name,
        workspace="project",
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


async def test_merged_pull_request_is_ignored() -> None:
    now = datetime.now(UTC)
    payload = _payload(action="closed", changes={})
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["merged"] = True
    raw_body, headers = _signed_request(payload, "pull_request")

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.action == "merged"
    assert event.ignored_reason == "pull_request_closed"
    assert await prepare_github_webhook_event(event, config=_config()) is event


def test_unmerged_closed_pull_request_is_ignored() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(action="closed", changes={}),
        "pull_request",
    )

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.action == "closed"
    assert event.ignored_reason == "pull_request_closed"


def test_ci_failure_maps_to_actionable_local_follow_up() -> None:
    now = datetime.now(UTC)
    payload = _payload(action="completed", changes={})
    payload["check_run"] = {
        "name": "test",
        "conclusion": "failure",
        "pull_requests": [{"number": 42}],
    }
    raw_body, headers = _signed_request(payload, "check_run")

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.host_message is None
    assert event.instructions
    assert event.external_context is not None
    assert event.external_context["pull_request_url"] == (
        "https://github.com/example/project/pull/42"
    )


def test_changes_requested_review_maps_to_actionable_follow_up() -> None:
    now = datetime.now(UTC)
    payload = _payload(action="submitted", changes={})
    del payload["number"]
    payload["review"] = {"state": "changes_requested"}
    raw_body, headers = _signed_request(payload, "pull_request_review")

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.host_message is None
    assert event.instructions
    assert event.external_context is not None
    assert event.external_context["event"] == "pull_request_review"


def test_approved_review_remains_a_literal_notification() -> None:
    now = datetime.now(UTC)
    payload = _payload(action="submitted", changes={})
    payload["review"] = {"state": "approved"}
    raw_body, headers = _signed_request(payload, "pull_request_review")

    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is None
    assert event.host_message == (
        "GitHub PR update — example/project#42: review submitted (approved).\n"
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


def test_routes_use_linear_workspaces_without_repository_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhooks.configured_linear_workspace_names",
        lambda _tool: ("project",),
    )
    options = GitHubPluginOptions.model_validate(
        {"webhook_routes": [{"name": "project", "repository": _REPOSITORY, "workspace": "legacy"}]}
    )
    routes = github_webhook_routes(options.webhook_routes)

    assert [(route.path, route.workspace) for route in routes] == [
        ("/webhooks/github/project", None)
    ]
    assert routes[0].candidate_workspaces == ("project",)
    assert routes[0].prepare_event is not None
    assert routes[0].routes_conversations is True


def test_sender_allowlist_marks_route_trusted_and_allows_admin_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhooks.configured_linear_workspace_names",
        lambda _tool: ("project",),
    )
    config = GitHubWebhookRouteConfig(
        name="project",
        repository=_REPOSITORY,
        allowed_senders=("repo-owner",),
    )

    route = github_webhook_routes((config,))[0]

    assert route.public_source is False
    assert route.allow_admin_workspaces is True


@asynccontextmanager
async def _linear_client_context(client: object):
    yield client


@pytest.mark.parametrize("event_type", ["issue_comment", "pull_request_review"])
@pytest.mark.parametrize(
    ("allowed_senders", "public_source"),
    [((), True), (("repo-owner",), False)],
)
async def test_actionable_pr_feedback_routes_to_linked_linear_conversation(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    allowed_senders: tuple[str, ...],
    public_source: bool,
) -> None:
    config = GitHubWebhookRouteConfig(
        name="project",
        repository=_REPOSITORY,
        allowed_senders=allowed_senders,
    )
    now = datetime.now(UTC)
    if event_type == "issue_comment":
        payload = _payload(action="created", changes={})
        payload.pop("pull_request")
        payload["issue"] = {"number": 42, "pull_request": {}}
    else:
        payload = _payload(action="submitted", changes={})
        payload["review"] = {"state": "commented"}
    raw_body, headers = _signed_request(payload, event_type)
    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=config)
    linear_client = AsyncMock()
    linear_client.find_issues_by_attachment_url.return_value = [
        {"issue": {"id": "issue-1", "project": {"id": "project-1"}}}
    ]
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:linear:issue"),
        key=ConversationSubjectKey("issue-1"),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.linear_client",
        lambda **_kwargs: _linear_client_context(linear_client),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.workspace_for_linear_project",
        lambda _project_id: "project",
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.workspace_issue",
        AsyncMock(
            return_value=(
                {"id": "issue-1", "identifier": "SYN-1", "title": "Ship the fix"},
                object(),
            )
        ),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.find_linear_issue_control_conversation",
        AsyncMock(return_value=(_Conversation(subject=subject), _Control())),
    )

    prepared = await prepare_github_webhook_event(event, config=config)

    assert prepared.conversation is not None
    assert prepared.conversation.subject == subject
    assert prepared.conversation.workspace == "project"
    assert prepared.conversation.control_title == "[SYN-1] Ship the fix"
    assert prepared.conversation.public_source is public_source
    assert prepared.external_context is not None
    linear_client.find_issues_by_attachment_url.assert_awaited_once_with(
        "https://github.com/example/project/pull/42"
    )


async def test_unlinked_actionable_review_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    payload = _payload(action="created", changes={})
    del payload["number"]
    raw_body, headers = _signed_request(payload, "pull_request_review_comment")
    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    linear_client = AsyncMock()
    linear_client.find_issues_by_attachment_url.return_value = []
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.linear_client",
        lambda **_kwargs: _linear_client_context(linear_client),
    )

    prepared = await prepare_github_webhook_event(event, config=_config())

    assert prepared.conversation is None
    assert prepared.instructions is None
    assert prepared.host_message is None
    assert prepared.ignored_reason == "pull_request_has_no_managed_linear_issue"
    for attachment in ({"issue": {}}, {"issue": {"id": "issue-1", "project": {}}}):
        linear_client.find_issues_by_attachment_url.return_value = [attachment]
        prepared = await prepare_github_webhook_event(event, config=_config())
        assert prepared.ignored_reason == "pull_request_has_no_managed_linear_issue"


async def test_multi_pr_check_notification_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = WebhookEvent(
        delivery_id=_DELIVERY_ID,
        event_type="check_run",
        action="completed",
        subject_id="41,42",
        occurred_at=datetime.now(UTC).isoformat(),
        instructions=None,
        external_context=None,
        host_message="Two PRs failed.",
    )
    linear_client = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.linear_client",
        lambda **_kwargs: _linear_client_context(linear_client),
    )

    prepared = await prepare_github_webhook_event(event, config=_config())

    assert prepared.ignored_reason == "github_event_has_no_single_pull_request"
    linear_client.find_issues_by_attachment_url.assert_not_awaited()


async def test_linked_feedback_without_existing_control_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    payload = _payload(action="submitted", changes={})
    payload["review"] = {"state": "commented"}
    raw_body, headers = _signed_request(payload, "pull_request_review")
    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear._linked_issue_for_pr",
        AsyncMock(
            return_value=(
                "issue-1",
                "project",
                {"id": "issue-1", "identifier": "SYN-1", "title": "Ship the fix"},
            )
        ),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.find_linear_issue_control_conversation",
        AsyncMock(return_value=None),
    )

    prepared = await prepare_github_webhook_event(event, config=_config())

    assert prepared.conversation is None
    assert prepared.instructions is None
    assert prepared.host_message is None
    assert prepared.ignored_reason == "linear_issue_has_no_existing_control"


async def test_non_actionable_event_is_left_unchanged() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(), "pull_request")
    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    non_actionable = replace(event, event_type="push")

    prepared = await prepare_github_webhook_event(non_actionable, config=_config())

    assert prepared is non_actionable


async def test_pr_notification_routes_to_existing_linear_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(), "pull_request")
    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:linear:issue"),
        key=ConversationSubjectKey("issue-1"),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear._linked_issue_for_pr",
        AsyncMock(
            return_value=(
                "issue-1",
                "project",
                {"id": "issue-1", "identifier": "SYN-1", "title": "Ship the fix"},
            )
        ),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.find_linear_issue_control_conversation",
        AsyncMock(return_value=(_Conversation(subject=subject), _Control())),
    )

    prepared = await prepare_github_webhook_event(event, config=_config())

    assert prepared.conversation is not None
    assert prepared.conversation.subject == subject
    assert prepared.conversation.workspace == "project"
    assert prepared.conversation.notification_jid == "discord:channel:linear-issue"
    assert prepared.host_message == event.host_message


async def test_pr_notification_delivery_posts_to_existing_linear_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SIGNING_KEY)
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:linear:issue"),
        key=ConversationSubjectKey("issue-1"),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear._linked_issue_for_pr",
        AsyncMock(
            return_value=(
                "issue-1",
                "project",
                {"id": "issue-1", "identifier": "SYN-1", "title": "Ship the fix"},
            )
        ),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.find_linear_issue_control_conversation",
        AsyncMock(return_value=(_Conversation(subject=subject), _Control())),
    )
    config = _config()
    route = WebhookRoute(
        provider="github",
        name=config.name,
        workspace=None,
        candidate_workspaces=("project",),
        secret_env=config.secret_env,
        parse=partial(parse_github_webhook, config=config),
        prepare_event=partial(prepare_github_webhook_event, config=config),
    )
    deps = _WebhookDeps()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        raw_body, headers = _signed_request(_payload(), "pull_request")
        response = await client.post(route.path, data=raw_body, headers=headers)
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body == {"status": "notified", "duplicate": False}
    assert deps.host_messages == [
        (
            "discord:channel:linear-issue",
            (
                "GitHub PR update — example/project#42: PR description updated.\n"
                "https://github.com/example/project/pull/42"
            ),
        )
    ]
    assert not deps.dispatched


async def test_off_board_linked_issue_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    payload = _payload()
    payload["review"] = {"state": "commented"}
    raw_body, headers = _signed_request(payload, "pull_request_review")
    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    linear_client = AsyncMock()
    linear_client.find_issues_by_attachment_url.side_effect = LinearWorkspaceIssueError("bad issue")
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.linear_client",
        lambda **_kwargs: _linear_client_context(linear_client),
    )

    prepared = await prepare_github_webhook_event(event, config=_config())

    assert prepared.conversation is None
    assert prepared.ignored_reason == "linear_issue_is_not_on_managed_board"


@pytest.mark.parametrize("provider_error", [LinearError("down")])
async def test_linked_issue_provider_failures_are_reported(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: Exception,
) -> None:
    now = datetime.now(UTC)
    payload = _payload()
    payload["review"] = {"state": "commented"}
    raw_body, headers = _signed_request(payload, "pull_request_review")
    event = parse_github_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    linear_client = AsyncMock()
    linear_client.find_issues_by_attachment_url.side_effect = provider_error
    monkeypatch.setattr(
        "pynchy.plugins.integrations.github_webhook_linear.linear_client",
        lambda **_kwargs: _linear_client_context(linear_client),
    )

    with pytest.raises(WebhookProcessingError, match="down"):
        await prepare_github_webhook_event(event, config=_config())


class _WebhookDeps:
    def __init__(self) -> None:
        self.broadcast_synthetic_user_input = AsyncMock()
        self.capability_status_operations = AsyncMock()
        self.deploy_operations = object()
        self.canary_run_to_dict = lambda *_args, **_kwargs: {}
        self.work_item_execution_to_dict = lambda *_args, **_kwargs: {}
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

    async def get_canary_report(self, *, history_limit: int) -> dict[str, object]:
        return {}

    def get_plugin_manager(self) -> object:
        return object()

    def get_workspace(self, folder: str) -> WorkspaceProfile | None:
        return self.workspace if folder == self.workspace.folder else None

    def dispatch_scheduled_task(self, task: ScheduledTask) -> None:
        self.dispatched.append(task)


def _public_runtime() -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        bind_host="0.0.0.0",  # noqa: S104 - exercise public-bind auth policy
        port=8484,
        unix_socket=None,
        public_bind=True,
        remote_auth_required=True,
        allow_remote_deploy=False,
        auth_token=ControlPlaneToken("control-plane-token-that-is-long-enough"),
        rate_limiter=RequestRateLimiter(request_limit=100, window_seconds=60),
        audit_security_event=AsyncMock(),
    )


async def test_unlisted_sender_is_discarded_without_a_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SIGNING_KEY)
    config = GitHubWebhookRouteConfig(
        name="project",
        repository=_REPOSITORY,
        allowed_senders=("repo-owner",),
    )
    route = WebhookRoute(
        provider="github",
        name=config.name,
        workspace="project",
        secret_env=config.secret_env,
        parse=partial(parse_github_webhook, config=config),
    )
    deps = _WebhookDeps()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        raw_body, headers = _signed_request(
            _payload(sender="drive-by"),
            "pull_request",
        )
        response = await client.post(route.path, data=raw_body, headers=headers)
    finally:
        await client.close()

    assert response.status == 204
    assert await get_webhook_receipt("github", "project", _DELIVERY_ID) is None
    assert not deps.dispatched
    assert not deps.host_messages


async def test_merged_delivery_creates_no_task_or_notification(
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
    raw_body, headers = _signed_request(payload, "pull_request")
    event = parse_github_webhook(
        raw_body,
        headers,
        _SIGNING_KEY,
        datetime.now(UTC),
        config=_config(),
    )
    assert event.ignored_reason == "pull_request_closed"
    try:
        response = await client.post(
            "/webhooks/github/project",
            data=raw_body,
            headers=headers,
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body == {"status": "ignored", "duplicate": False}
    assert not deps.dispatched
    assert not deps.host_messages
