"""Public contracts for plugin-provided webhook routes."""

from __future__ import annotations

import pluggy
import pytest

from pynchy.plugins.api import PynchySpec
from pynchy.plugins.webhooks import (
    WebhookConfigurationError,
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


def test_webhook_event_and_lifecycle_reject_ambiguous_untrusted_payloads() -> None:
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
