"""Tests for LiteLLM Responses-mode route selection and canaries."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from pynchy.host.container_manager.gateway import LiteLLMGateway
from pynchy.host.container_manager.litellm_config import (
    LiteLLMConfigPreparer,
    ResponseModelRoute,
)

if TYPE_CHECKING:
    from pathlib import Path


_LITELLM_MOD = "pynchy.host.container_manager.gateway_litellm"
_DOCKER_MOD = "pynchy.host.container_manager.docker"
_RESPONSES_MOD = "pynchy.host.container_manager.litellm_responses"
_LITELLM_KWARGS = {
    "port": 4000,
    "container_host": "host.docker.internal",
    "image": "ghcr.io/berriai/litellm:main-latest",
    "postgres_image": "postgres:17-alpine",
    "master_key": "test-master-key",
}


class TestPrepareLiteLLMResponsesConfig:
    def test_collects_all_launchable_responses_routes_with_duplicate_count(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            "  - model_name: responses-model\n"
            "    model_info:\n"
            "      mode: responses\n"
            "    litellm_params:\n"
            "      model: openai/one\n"
            "  - model_name: responses-model\n"
            "    model_info:\n"
            "      mode: responses\n"
            "    litellm_params:\n"
            "      model: openai/two\n"
            "  - model_name: responses-model\n"
            "    model_info:\n"
            "      mode: responses\n"
            "    litellm_params:\n"
            "      model: openai/filtered\n"
            "      api_key: os.environ/MISSING_KEY\n"
            "  - model_name: responses-model\n"
            "    model_info:\n"
            "      mode: chat\n"
            "    litellm_params:\n"
            "      model: openai/chat-only\n"
            "  - model_name: second-responses-model\n"
            "    model_info:\n"
            "      mode: responses\n"
            "    litellm_params:\n"
            "      model: openai/three\n"
        )

        prepared = LiteLLMConfigPreparer(
            required_response_models=("responses-model", "responses-model")
        ).prepare(cfg, tmp_path, env={})

        assert prepared.response_routes == (
            ResponseModelRoute(
                model="responses-model",
                route_count=2,
                canary_model="responses-model",
            ),
            ResponseModelRoute(
                model="second-responses-model",
                route_count=1,
                canary_model="second-responses-model",
            ),
        )

    def test_required_responses_model_can_match_provider_wildcard_route(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            "  - model_name: openai/*\n"
            "    model_info:\n"
            "      mode: responses\n"
            "    litellm_params:\n"
            "      model: openai/*\n"
        )

        prepared = LiteLLMConfigPreparer(required_response_models=("openai/gpt-5.5",)).prepare(
            cfg, tmp_path, env={}
        )

        assert prepared.response_routes == (
            ResponseModelRoute(
                model="openai/*",
                route_count=1,
                canary_model="openai/gpt-5.5",
            ),
        )

    def test_raises_when_required_responses_route_is_missing(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            "  - model_name: responses-model\n"
            "    model_info:\n"
            "      mode: chat\n"
            "    litellm_params:\n"
            "      model: openai/chat-only\n"
        )

        with pytest.raises(
            RuntimeError,
            match=r"Configured Responses model route\(s\) missing.*responses-model",
        ):
            LiteLLMConfigPreparer(required_response_models=("responses-model",)).prepare(
                cfg,
                tmp_path,
                env={},
            )


class TestLiteLLMResponsesAvailability:
    @staticmethod
    def _response_session(
        *,
        status: int = 200,
        lines: tuple[bytes, ...] = (b"data: [DONE]\n\n",),
        error: Exception | None = None,
        requests: list[dict[str, object]] | None = None,
    ):
        response = MagicMock()
        response.status = status
        response.content = MagicMock()
        response.content.__aiter__.return_value = lines

        @asynccontextmanager
        async def post(url: str, **kwargs: object):
            if requests is not None:
                requests.append({"url": url, **kwargs})
            if error is not None:
                raise error
            yield response

        session = MagicMock(spec=aiohttp.ClientSession)
        session.post = post

        @asynccontextmanager
        async def session_context(*_args: object, **_kwargs: object):
            yield session

        return session_context

    @staticmethod
    async def _start_gateway(
        tmp_path: Path,
        session_context,
        *,
        response_models: tuple[str, ...] = ("responses-model",),
        required_response_models: tuple[str, ...] | None = None,
    ) -> LiteLLMGateway:
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text(
            "model_list:\n"
            + "".join(
                "  - model_name: "
                f"{model}\n"
                "    model_info:\n"
                "      mode: responses\n"
                "    litellm_params:\n"
                f"      model: openai/{model}\n"
                for model in response_models
            )
        )
        gateway = LiteLLMGateway(
            config_path=str(cfg),
            data_dir=tmp_path,
            required_response_models=(
                required_response_models
                if required_response_models is not None
                else response_models
            ),
            **_LITELLM_KWARGS,
        )

        with (
            patch(f"{_LITELLM_MOD}.docker_available", return_value=True),
            patch(f"{_LITELLM_MOD}.run_docker", new_callable=AsyncMock),
            patch(f"{_DOCKER_MOD}.run_docker", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_image", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.ensure_network", new_callable=AsyncMock),
            patch.object(gateway, "_wait_postgres_healthy", new_callable=AsyncMock),
            patch(f"{_LITELLM_MOD}.wait_healthy", new_callable=AsyncMock),
            patch(f"{_RESPONSES_MOD}.aiohttp.ClientSession", session_context),
        ):
            await gateway.start()
        return gateway

    @pytest.mark.asyncio
    async def test_start_probes_responses_alias_with_bounded_sse_canary(self, tmp_path: Path):
        requests: list[dict[str, object]] = []
        gateway = await self._start_gateway(
            tmp_path,
            self._response_session(
                lines=(b"data: response.created\n\n", b"data: [DONE]\n\n"),
                requests=requests,
            ),
        )

        status = gateway.responses_status

        assert status["state"] == "available"
        assert status["stale"] is False
        assert status["aliases"] == [
            {
                "alias": "responses-model",
                "route_count": 1,
                "state": "available",
                "checked_at": status["checked_at"],
                "failure": None,
            }
        ]
        assert requests == [
            {
                "url": "http://localhost:4000/v1/responses",
                "headers": {
                    "Authorization": "Bearer test-master-key",
                    "Accept": "text/event-stream",
                    "Content-Type": "application/json",
                },
                "json": {
                    "model": "responses-model",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "."}],
                        }
                    ],
                    "stream": True,
                    "max_output_tokens": 1,
                },
            }
        ]

    @pytest.mark.asyncio
    async def test_start_probes_every_launchable_responses_alias(self, tmp_path: Path):
        requests: list[dict[str, object]] = []
        gateway = await self._start_gateway(
            tmp_path,
            self._response_session(requests=requests),
            response_models=("required-responses-model", "other-responses-model"),
            required_response_models=("required-responses-model",),
        )

        canary_models: list[str] = []
        for request in requests:
            payload = request["json"]
            assert isinstance(payload, dict)
            model = payload["model"]
            assert isinstance(model, str)
            canary_models.append(model)

        assert canary_models == ["required-responses-model", "other-responses-model"]
        assert gateway.responses_status["state"] == "available"

    @pytest.mark.asyncio
    async def test_start_probes_wildcard_alias_with_matching_required_model(self, tmp_path: Path):
        requests: list[dict[str, object]] = []
        gateway = await self._start_gateway(
            tmp_path,
            self._response_session(requests=requests),
            response_models=("openai/*",),
            required_response_models=("openai/gpt-5.5",),
        )

        payload = requests[0]["json"]

        assert isinstance(payload, dict)
        assert payload["model"] == "openai/gpt-5.5"
        assert gateway.responses_status["aliases"] == [
            {
                "alias": "openai/*",
                "route_count": 1,
                "state": "available",
                "checked_at": gateway.responses_status["checked_at"],
                "failure": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_start_does_not_probe_wildcard_alias_without_concrete_model(self, tmp_path: Path):
        requests: list[dict[str, object]] = []
        gateway = await self._start_gateway(
            tmp_path,
            self._response_session(requests=requests),
            response_models=("openai/*",),
            required_response_models=(),
        )

        assert requests == []
        assert gateway.responses_status["state"] == "unavailable"
        assert gateway.responses_status["aliases"] == [
            {
                "alias": "openai/*",
                "route_count": 1,
                "state": "unavailable",
                "checked_at": gateway.responses_status["checked_at"],
                "failure": "not_probeable",
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "lines", "failure"),
        [
            (503, (b"data: raw-upstream-body\n\n",), "http_5xx"),
            (200, (b"data: response.created\n\n",), "protocol"),
        ],
    )
    async def test_start_marks_non_terminal_or_non_200_canary_unavailable(
        self,
        tmp_path: Path,
        status: int,
        lines: tuple[bytes, ...],
        failure: str,
    ):
        gateway = await self._start_gateway(
            tmp_path,
            self._response_session(status=status, lines=lines),
        )

        responses = gateway.responses_status

        assert responses["state"] == "unavailable"
        assert responses["aliases"][0]["failure"] == failure

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "failure"),
        [
            (TimeoutError("provider timeout"), "timeout"),
            (aiohttp.ClientError("provider disconnected"), "network"),
        ],
    )
    async def test_start_sanitizes_transport_canary_failures(
        self,
        tmp_path: Path,
        error: Exception,
        failure: str,
    ):
        gateway = await self._start_gateway(tmp_path, self._response_session(error=error))

        responses = gateway.responses_status

        assert responses["state"] == "unavailable"
        assert responses["aliases"][0]["failure"] == failure

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_type", [aiohttp.ClientError, RuntimeError])
    async def test_responses_status_excludes_raw_provider_failure_details(
        self,
        tmp_path: Path,
        error_type: type[Exception],
    ):
        raw_failure = (
            "raw-body=DO_NOT_LEAK authorization=Bearer super-secret "
            "session_id=session-private prompt=private-prompt"
        )
        gateway = await self._start_gateway(
            tmp_path,
            self._response_session(error=error_type(raw_failure)),
        )

        rendered = json.dumps(gateway.responses_status)

        for forbidden in (
            "DO_NOT_LEAK",
            "Bearer super-secret",
            "session-private",
            "private-prompt",
            "test-master-key",
        ):
            assert forbidden not in rendered
