"""Public behavior contracts for LiteLLM config preparation edge cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from pynchy.host.container_manager.litellm_config import LiteLLMConfigPreparer

if TYPE_CHECKING:
    from pathlib import Path


def test_copies_non_mapping_config_when_no_routes_are_required(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    original = "- provider-only-entry\n"
    cfg.write_text(original)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    prepared = LiteLLMConfigPreparer().prepare(cfg, runtime, env={})

    assert prepared.path.read_text() == original


def test_rejects_missing_model_list_when_a_route_is_required(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text("general_settings:\n  drop_params: true\n")

    with pytest.raises(RuntimeError, match="does not declare model_list"):
        LiteLLMConfigPreparer(required_models=("required-model",)).prepare(
            cfg,
            tmp_path / "runtime",
            env={},
        )


def test_rejects_missing_model_list_when_a_responses_route_is_required(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text("general_settings:\n  drop_params: true\n")

    with pytest.raises(RuntimeError, match="required Responses model"):
        LiteLLMConfigPreparer(required_response_models=("responses-model",)).prepare(
            cfg,
            tmp_path / "runtime",
            env={},
        )


def test_filters_placeholder_credentials_but_keeps_usable_routes(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text(
        "model_list:\n"
        "  - model_name: usable\n"
        "    litellm_params:\n"
        "      model: openai/usable\n"
        "  - model_name: placeholder\n"
        "    litellm_params:\n"
        "      model: openai/placeholder\n"
        "      api_key: os.environ/PLACEHOLDER_KEY\n"
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    prepared = LiteLLMConfigPreparer().prepare(
        cfg,
        runtime,
        env={"PLACEHOLDER_KEY": "YOUR_OPENAI_KEY"},
    )

    filtered = yaml.safe_load(prepared.path.read_text())
    assert [entry["model_name"] for entry in filtered["model_list"]] == ["usable"]


def test_keeps_a_route_with_a_non_placeholder_credential(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text(
        "model_list:\n"
        "  - model_name: usable\n"
        "    litellm_params:\n"
        "      model: openai/usable\n"
        "      api_key: os.environ/VALID_KEY\n"
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    prepared = LiteLLMConfigPreparer().prepare(
        cfg,
        runtime,
        env={"VALID_KEY": "real-provider-key"},
    )

    filtered = yaml.safe_load(prepared.path.read_text())
    assert [entry["model_name"] for entry in filtered["model_list"]] == ["usable"]


def test_required_model_rejects_a_non_string_model_name(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text(
        "model_list:\n  - model_name: 123\n    litellm_params:\n      model: openai/numbered\n"
    )

    with pytest.raises(RuntimeError, match="required-model"):
        LiteLLMConfigPreparer(required_models=("required-model",)).prepare(
            cfg,
            tmp_path / "runtime",
            env={},
        )


def test_rejects_non_mapping_litellm_settings(tmp_path: Path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text(
        "model_list:\n"
        "  - model_name: usable\n"
        "    litellm_params:\n"
        "      model: openai/usable\n"
        "litellm_settings: []\n"
    )

    with pytest.raises(TypeError, match="litellm_settings must be a mapping"):
        LiteLLMConfigPreparer().prepare(cfg, tmp_path / "runtime", env={})
