"""Tests for required LiteLLM Responses-mode routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.host.container_manager.litellm_config import LiteLLMConfigPreparer

if TYPE_CHECKING:
    from pathlib import Path


def test_accepts_exact_required_responses_route(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text(
        "model_list:\n"
        "  - model_name: responses-model\n"
        "    model_info:\n"
        "      mode: responses\n"
        "    litellm_params:\n"
        "      model: openai/responses-model\n"
    )

    LiteLLMConfigPreparer(required_response_models=("responses-model",)).prepare(
        cfg, tmp_path, env={}
    )


def test_required_responses_model_can_match_provider_wildcard_route(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text(
        "model_list:\n"
        "  - model_name: openai/*\n"
        "    model_info:\n"
        "      mode: responses\n"
        "    litellm_params:\n"
        "      model: openai/*\n"
    )

    LiteLLMConfigPreparer(required_response_models=("openai/gpt-5.5",)).prepare(
        cfg, tmp_path, env={}
    )


def test_raises_when_required_responses_route_is_missing(tmp_path: Path):
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
