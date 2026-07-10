from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from pynchy.conversation import factory
from pynchy.conversation.factory import resolved_phoenix_endpoint
from pynchy.conversation.sink import ConversationSink

if TYPE_CHECKING:
    import pytest


def _settings(
    *,
    project_name: str = "pynchy",
    phoenix_endpoint: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_store=SimpleNamespace(
            project_name=project_name,
            phoenix_endpoint=phoenix_endpoint,
        )
    )


def _install_factory_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: SimpleNamespace,
) -> tuple[list[tuple[str, str | None]], list[object]]:
    tracer_calls: list[tuple[str, str | None]] = []
    store_tracers: list[object] = []

    def fake_get_settings() -> SimpleNamespace:
        return settings

    def fake_phoenix_tracer(project_name: str, endpoint: str | None = None) -> object:
        tracer_calls.append((project_name, endpoint))
        return object()

    class FakePhoenixConversationStore:
        def __init__(self, *, tracer: object) -> None:
            store_tracers.append(tracer)

    monkeypatch.setattr(factory, "get_settings", fake_get_settings)
    monkeypatch.setattr(factory, "phoenix_tracer", fake_phoenix_tracer)
    monkeypatch.setattr(factory, "PhoenixConversationStore", FakePhoenixConversationStore)
    return tracer_calls, store_tracers


def test_endpoint_prefers_base_collector_endpoint() -> None:
    env = {
        "PHOENIX_COLLECTOR_ENDPOINT": "https://phoenix.example.com",
        "PHOENIX_COLLECTOR_HTTP_ENDPOINT": "https://wrong.example.com/v1/traces",
    }
    assert resolved_phoenix_endpoint(env) == "https://phoenix.example.com"


def test_endpoint_derives_base_from_litellm_http_endpoint() -> None:
    env = {"PHOENIX_COLLECTOR_HTTP_ENDPOINT": "https://phoenix.example.com/v1/traces"}
    assert resolved_phoenix_endpoint(env) == "https://phoenix.example.com"


def test_endpoint_ignores_http_endpoint_without_host() -> None:
    env = {"PHOENIX_COLLECTOR_HTTP_ENDPOINT": "/v1/traces"}
    assert resolved_phoenix_endpoint(env) is None


def test_endpoint_derives_base_from_http_endpoint_with_trailing_slash() -> None:
    env = {"PHOENIX_COLLECTOR_HTTP_ENDPOINT": "https://phoenix.example.com/v1/traces/"}
    assert resolved_phoenix_endpoint(env) == "https://phoenix.example.com"


def test_endpoint_ignores_base_collector_endpoint_without_host() -> None:
    env = {"PHOENIX_COLLECTOR_ENDPOINT": "////"}
    assert resolved_phoenix_endpoint(env) is None


def test_endpoint_falls_through_when_base_collector_endpoint_has_no_host() -> None:
    env = {
        "PHOENIX_COLLECTOR_ENDPOINT": "////",
        "PHOENIX_COLLECTOR_HTTP_ENDPOINT": "https://phoenix.example.com/v1/traces",
    }
    assert resolved_phoenix_endpoint(env) == "https://phoenix.example.com"


def test_build_sink_prefers_normalized_config_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "https://env.example.com")
    tracer_calls, store_tracers = _install_factory_fakes(
        monkeypatch,
        settings=_settings(
            project_name="conversation-project",
            phoenix_endpoint=" https://phoenix.example.com/v1/traces/ ",
        ),
    )

    sink = factory.build_conversation_sink()

    assert isinstance(sink, ConversationSink)
    assert tracer_calls == [("conversation-project", "https://phoenix.example.com")]
    assert len(store_tracers) == 1


def test_build_sink_uses_env_fallback_when_config_endpoint_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    monkeypatch.setenv("PHOENIX_COLLECTOR_HTTP_ENDPOINT", "https://env.example.com/v1/traces")
    tracer_calls, _store_tracers = _install_factory_fakes(
        monkeypatch,
        settings=_settings(project_name="conversation-project", phoenix_endpoint=None),
    )

    sink = factory.build_conversation_sink()

    assert isinstance(sink, ConversationSink)
    assert tracer_calls == [("conversation-project", "https://env.example.com")]


def test_build_sink_falls_back_to_env_when_config_endpoint_has_no_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    monkeypatch.setenv("PHOENIX_COLLECTOR_HTTP_ENDPOINT", "https://env.example.com/v1/traces")
    tracer_calls, _store_tracers = _install_factory_fakes(
        monkeypatch,
        settings=_settings(project_name="conversation-project", phoenix_endpoint=" //// "),
    )

    sink = factory.build_conversation_sink()

    assert isinstance(sink, ConversationSink)
    assert tracer_calls == [("conversation-project", "https://env.example.com")]
