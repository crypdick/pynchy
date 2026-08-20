"""Filtering and validation for user-managed LiteLLM proxy config."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - beartype resolves LiteLLM config paths at runtime.
from typing import Any

import yaml

from pynchy.logger import logger

PLACEHOLDER_RE = re.compile(r"\.\.\.|YOUR_|CHANGE_ME|REPLACE_|xxx{3,}", re.IGNORECASE)
_MODEL_LIST_TYPE_ERROR = "LiteLLM config model_list must be a list"
_MODEL_LIST_ENTRY_TYPE_ERROR = "LiteLLM config model_list entries must be mappings"
_LITELLM_SETTINGS_TYPE_ERROR = "LiteLLM config litellm_settings must be a mapping"


@dataclass(frozen=True)
class PreparedLiteLLMConfig:
    """Generated LiteLLM config."""

    path: Path


@dataclass(frozen=True)
class LiteLLMConfigPreparer:
    """Prepare the mounted LiteLLM config from the user-managed source file."""

    required_models: tuple[str, ...] = ()
    required_response_models: tuple[str, ...] = ()

    def prepare(
        self,
        config_path: Path,
        output_dir: Path,
        env: dict[str, str],
    ) -> PreparedLiteLLMConfig:
        config_text = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(config_text)

        if not isinstance(config, dict):
            return _copy_unvalidated_config(
                config_path,
                output_dir,
                config_text,
                self.required_models,
                self.required_response_models,
            )
        if "model_list" not in config:
            if self.required_models or self.required_response_models:
                return _copy_unvalidated_config(
                    config_path,
                    output_dir,
                    config_text,
                    self.required_models,
                    self.required_response_models,
                )
            return PreparedLiteLLMConfig(path=_write_filtered_config(output_dir, config))

        model_list = _model_list(config)
        kept, removed_reasons = _filter_model_list(model_list, env)
        config["model_list"] = kept
        _log_filter_result(original_count=len(model_list), remaining=len(kept))
        _require_usable_routes(config_path, kept, removed_reasons)
        _validate_required_models(self.required_models, kept)
        _validate_required_response_models(self.required_response_models, kept)
        return PreparedLiteLLMConfig(path=_write_filtered_config(output_dir, config))


def _copy_unvalidated_config(
    _config_path: Path,
    output_dir: Path,
    config_text: str,
    required_models: tuple[str, ...],
    required_response_models: tuple[str, ...],
) -> PreparedLiteLLMConfig:
    if required_models:
        models = ", ".join(required_models)
        msg = f"LiteLLM config does not declare model_list for required model(s): {models}"
        raise RuntimeError(msg)
    if required_response_models:
        models = ", ".join(required_response_models)
        msg = (
            f"LiteLLM config does not declare model_list for required Responses model(s): {models}"
        )
        raise RuntimeError(msg)
    out = output_dir / "litellm_config.yaml"
    out.write_text(config_text, encoding="utf-8")
    return PreparedLiteLLMConfig(path=out)


def _model_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    model_list = config["model_list"]
    if not isinstance(model_list, list):
        raise TypeError(_MODEL_LIST_TYPE_ERROR)
    if not all(isinstance(entry, dict) for entry in model_list):
        raise TypeError(_MODEL_LIST_ENTRY_TYPE_ERROR)
    return model_list


def _filter_model_list(
    model_list: list[dict[str, Any]],
    env: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    kept: list[dict[str, Any]] = []
    removed_reasons: list[str] = []
    for entry in model_list:
        reason = _entry_filter_reason(entry, env)
        if reason is None:
            kept.append(entry)
        else:
            removed_reasons.append(reason)
    return kept, removed_reasons


def _entry_filter_reason(entry: dict[str, Any], env: dict[str, str]) -> str | None:
    api_key = (entry.get("litellm_params") or {}).get("api_key", "")
    match = re.match(r"os\.environ/(\w+)", str(api_key))
    if match is None:
        return None

    var_name = match.group(1)
    value = env.get(var_name)
    if not value:
        _log_removed_entry(entry, "unset api_key env var", var_name)
        return f"{_route_label(entry)} references unset {var_name}"
    if PLACEHOLDER_RE.search(value):
        _log_removed_entry(entry, "placeholder api_key", var_name)
        return f"{_route_label(entry)} references placeholder {var_name}"
    return None


def _log_removed_entry(entry: dict[str, Any], reason: str, var_name: str) -> None:
    model_id = (entry.get("model_info") or {}).get("id", "?")
    logger.warning(
        "Removing unusable model entry from LiteLLM config",
        reason=reason,
        model_id=model_id,
        var=var_name,
    )


def _route_label(entry: dict[str, Any]) -> str:
    model_id = (entry.get("model_info") or {}).get("id", "?")
    return str(entry.get("model_name") or model_id)


def _log_filter_result(*, original_count: int, remaining: int) -> None:
    removed = original_count - remaining
    if removed:
        logger.info(
            "Filtered litellm config",
            removed=removed,
            remaining=remaining,
        )


def _require_usable_routes(
    config_path: Path,
    kept: list[dict[str, Any]],
    removed_reasons: list[str],
) -> None:
    if kept:
        return
    reason = "; ".join(removed_reasons) if removed_reasons else "model_list is empty"
    msg = f"No usable LiteLLM model routes remain after filtering {config_path}: {reason}"
    raise RuntimeError(msg)


def _validate_required_models(
    required_models: tuple[str, ...],
    kept: list[dict[str, Any]],
) -> None:
    if not required_models:
        return
    enabled_names = {entry.get("model_name") for entry in kept}
    missing = [
        model
        for model in required_models
        if not any(
            _model_route_matches(model, configured_name) for configured_name in enabled_names
        )
    ]
    if not missing:
        return
    missing_text = ", ".join(missing)
    enabled_text = ", ".join(sorted(str(name) for name in enabled_names if name))
    msg = (
        "Configured agent model route(s) missing from enabled LiteLLM model_list: "
        f"{missing_text}. Enabled model_name values: {enabled_text or '<none>'}"
    )
    raise RuntimeError(msg)


def _is_responses_route(entry: dict[str, Any]) -> bool:
    model_info = entry.get("model_info")
    return isinstance(model_info, dict) and model_info.get("mode") == "responses"


def _validate_required_response_models(
    required_models: tuple[str, ...],
    model_list: list[dict[str, Any]],
) -> None:
    if not required_models:
        return
    available = tuple(
        dict.fromkeys(
            model
            for entry in model_list
            if _is_responses_route(entry)
            and isinstance((model := entry.get("model_name")), str)
            and model
        )
    )
    missing = [
        model
        for model in dict.fromkeys(required_models)
        if not any(_model_route_matches(model, configured_name) for configured_name in available)
    ]
    if not missing:
        return
    missing_text = ", ".join(missing)
    available_text = ", ".join(sorted(available))
    msg = (
        "Configured Responses model route(s) missing from enabled LiteLLM model_list: "
        f"{missing_text}. Responses model_name values: {available_text or '<none>'}"
    )
    raise RuntimeError(msg)


def _model_route_matches(required_model: str, configured_name: object) -> bool:
    if not isinstance(configured_name, str):
        return False
    if configured_name == required_model:
        return True
    if configured_name.endswith("/*"):
        return required_model.startswith(configured_name[:-1])
    return False


def _write_filtered_config(output_dir: Path, config: dict[str, Any]) -> Path:
    settings = config.setdefault("litellm_settings", {})
    if not isinstance(settings, dict):
        raise TypeError(_LITELLM_SETTINGS_TYPE_ERROR)
    settings["turn_off_message_logging"] = True
    settings["log_raw_request_response"] = False

    out = output_dir / "litellm_config.yaml"
    out.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return out
