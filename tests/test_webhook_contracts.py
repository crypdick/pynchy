"""Public contracts for plugin-provided webhook routes."""

from __future__ import annotations

from dataclasses import replace

import pluggy
import pytest

from pynchy.conversation.api import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.plugins.api import PynchySpec
from pynchy.plugins.webhooks import (
    WebhookConfigurationError,
    WebhookConversation,
    WebhookEvent,
    WebhookLifecycle,
    WebhookRoute,
    collect_webhook_routes,
    validate_webhook_routes,
)

hookimpl = pluggy.HookimplMarker("pynchy")


def _route(name: str = "events") -> WebhookRoute:
    def parse(*_args: object) -> WebhookEvent:
        raise AssertionError("Route validation must not parse provider requests")

    return WebhookRoute(
        provider="example",
        name=name,
        workspace="project",
        secret_env="WEBHOOK_TEST_SECRET",  # pragma: allowlist secret  # noqa: S106
        parse=parse,
    )


class _WebhookPlugin:
    def __init__(self, contribution: object) -> None:
        self._contribution = contribution

    @hookimpl
    def pynchy_webhook_routes(self) -> object:
        return self._contribution


def _plugin_manager(*contributions: object) -> pluggy.PluginManager:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    for contribution in contributions:
        manager.register(_WebhookPlugin(contribution))
    return manager


def test_validated_routes_require_distinct_public_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_TEST_SECRET", "secret")
    route = _route()

    assert validate_webhook_routes([route]) == (route,)
    with pytest.raises(WebhookConfigurationError, match="duplicate public paths"):
        validate_webhook_routes([route, _route()])


def test_route_validation_rejects_unsafe_or_unconfigured_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEBHOOK_TEST_SECRET", raising=False)
    route = _route()

    with pytest.raises(WebhookConfigurationError, match="requires environment variable"):
        validate_webhook_routes([route])

    monkeypatch.setenv("WEBHOOK_TEST_SECRET", "secret")
    unsafe_route = WebhookRoute(
        provider="Example",
        name="events",
        workspace="project",
        secret_env="WEBHOOK_TEST_SECRET",  # pragma: allowlist secret  # noqa: S106
        parse=route.parse,
    )
    with pytest.raises(WebhookConfigurationError, match="lowercase URL-safe"):
        validate_webhook_routes([unsafe_route])


def test_collect_routes_ignores_non_route_plugin_contributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBHOOK_TEST_SECRET", "secret")
    route = _route()

    routes = collect_webhook_routes(_plugin_manager(None, (route, "not-a-route")))

    assert routes == (route,)


def test_collect_routes_ignores_plugins_without_routes() -> None:
    assert collect_webhook_routes(_plugin_manager(None)) == ()


def test_collect_routes_skips_a_null_hook_result(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _plugin_manager()
    monkeypatch.setattr(manager.hook, "pynchy_webhook_routes", lambda: [None])

    assert collect_webhook_routes(manager) == ()


def test_webhook_event_and_lifecycle_reject_ambiguous_untrusted_payloads() -> None:
    with pytest.raises(ValueError, match="require instructions and context"):
        WebhookEvent(
            delivery_id="delivery-1",
            event_type="issue",
            action="updated",
            subject_id="issue-1",
            occurred_at="2026-07-29T00:00:00+00:00",
            instructions="review",
            external_context=None,
        )
    with pytest.raises(ValueError, match="isolated, routed, lifecycle-only"):
        WebhookEvent(
            delivery_id="delivery-1",
            event_type="issue",
            action="updated",
            subject_id="issue-1",
            occurred_at="2026-07-29T00:00:00+00:00",
            instructions=None,
            external_context=None,
        )
    with pytest.raises(ValueError, match="host notifications cannot be blank"):
        WebhookEvent(
            delivery_id="delivery-1",
            event_type="issue",
            action="updated",
            subject_id="issue-1",
            occurred_at="2026-07-29T00:00:00+00:00",
            instructions=None,
            external_context=None,
            host_message=" ",
        )
    with pytest.raises(ValueError, match="JSON serializable"):
        WebhookLifecycle(context={"provider": object()})


def test_webhook_conversation_rejects_blank_title_or_revision() -> None:
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear"),
        key=ConversationSubjectKey("issue-1"),
    )

    with pytest.raises(ValueError, match="control title cannot be blank"):
        WebhookConversation(subject=subject, control_title=" ")
    with pytest.raises(ValueError, match="control revision cannot be blank"):
        WebhookConversation(subject=subject, control_title="Issue", control_state_revision=" ")
    with pytest.raises(ValueError, match="notification JID cannot be blank"):
        WebhookConversation(subject=subject, control_title="Issue", notification_jid=" ")


def test_webhook_lifecycle_without_context_is_valid() -> None:
    assert WebhookLifecycle().context is None


def test_webhook_event_rejects_lifecycle_prompt_context_and_open_control() -> None:
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear"),
        key=ConversationSubjectKey("issue-1"),
    )
    conversation = WebhookConversation(subject=subject, control_title="Issue")

    with pytest.raises(ValueError, match="cannot carry prompt context"):
        WebhookEvent(
            delivery_id="delivery-1",
            event_type="issue",
            action="updated",
            subject_id="issue-1",
            occurred_at="2026-07-29T00:00:00+00:00",
            instructions="review",
            external_context={},
            conversation=conversation,
            lifecycle=WebhookLifecycle(),
        )
    with pytest.raises(ValueError, match="require a closed routed control"):
        WebhookEvent(
            delivery_id="delivery-1",
            event_type="issue",
            action="updated",
            subject_id="issue-1",
            occurred_at="2026-07-29T00:00:00+00:00",
            instructions=None,
            external_context=None,
            conversation=conversation,
            lifecycle=WebhookLifecycle(),
        )


@pytest.mark.parametrize(
    ("route", "message"),
    [
        (replace(_route(), workspace=" "), "blank workspace"),
        (replace(_route(), workspace=None), "no workspace candidates"),
        (
            replace(_route(), secret_env="not-valid"),  # pragma: allowlist secret  # noqa: S106
            "invalid secret environment",
        ),
        (replace(_route(), max_body_bytes=0), "no body-size budget"),
        (replace(_route(), rate_limit_requests=0), "invalid rate limit"),
    ],
)
def test_webhook_routes_reject_invalid_public_limits(
    monkeypatch: pytest.MonkeyPatch,
    route: WebhookRoute,
    message: str,
) -> None:
    monkeypatch.setenv("WEBHOOK_TEST_SECRET", "secret")

    with pytest.raises(WebhookConfigurationError, match=message):
        validate_webhook_routes([route])
