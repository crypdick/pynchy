"""Generated Codex CLI configuration for Pynchy sandboxes."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_SAFE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_PYNCHY_LITELLM_PROVIDER = "pynchy_litellm"
_UNSUPPORTED_TOML_VALUE_ERROR = "Unsupported TOML value: {value!r}"
_CODEX_GATEWAY_REQUIREMENTS_ERROR = "Codex core requires {requirements} from the Pynchy LLM gateway"
# Pynchy's container and mount policy are the isolation boundary. Codex's
# inner bubblewrap layer rejects tracked symlinked instruction dirs such as
# .agents -> .claude inside a writable project mount.
DEFAULT_CODEX_SANDBOX_MODE = "danger-full-access"


@dataclass(frozen=True)
class CodexModelSettings:
    """Model-specific settings for one generated Codex configuration."""

    model: str | None = None
    model_reasoning_effort: str | None = None


def _toml_key(key: str) -> str:
    """Return a TOML bare key when safe, otherwise a quoted key segment."""
    if _SAFE_TOML_KEY.fullmatch(key):
        return key
    return json.dumps(key)


def _toml_value(value: object) -> str:
    """Serialize the small TOML value subset this generated config needs."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(_UNSUPPORTED_TOML_VALUE_ERROR.format(value=value))


def _append_mapping_table(lines: list[str], name: str, values: dict[str, object] | None) -> None:
    if not values:
        return
    lines.extend(["", f"[{name}]"])
    for key, value in values.items():
        lines.append(f"{_toml_key(str(key))} = {_toml_value(str(value))}")


def _mcp_server_lines(name: str, spec: dict[str, object]) -> list[str]:
    """Render one Pynchy MCP server spec as Codex ``config.toml``."""
    server = f"mcp_servers.{_toml_key(name)}"
    direct_keys = (
        "command",
        "args",
        "cwd",
        "url",
        "startup_timeout_sec",
        "tool_timeout_sec",
        "enabled",
        "required",
        "default_tools_approval_mode",
        "enabled_tools",
        "disabled_tools",
        "bearer_token_env_var",
    )
    lines = ["", f"[{server}]"]
    for key in direct_keys:
        value = spec.get(key)
        if value is not None:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    if spec.get("auth_value_env") and spec.get("bearer_token_env_var") is None:
        lines.append(f"bearer_token_env_var = {_toml_value(spec['auth_value_env'])}")

    _append_mapping_table(lines, f"{server}.env", spec.get("env"))
    _append_mapping_table(
        lines, f"{server}.http_headers", spec.get("http_headers") or spec.get("headers")
    )
    _append_mapping_table(lines, f"{server}.env_http_headers", spec.get("env_http_headers"))

    return lines


def _append_v1(base_url: str) -> str:
    """Normalize a gateway root URL to the OpenAI ``/v1`` API base."""
    normalized = base_url.rstrip("/")
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def gateway_base_url_from_env() -> str:
    missing = [
        name
        for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(_CODEX_GATEWAY_REQUIREMENTS_ERROR.format(requirements=joined))
    return _append_v1(os.environ["OPENAI_BASE_URL"])


def _preserved_plugin_config(config_path: Path) -> str:
    """Keep native Codex marketplace and plugin state across Pynchy rewrites."""
    try:
        existing = config_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    sections: list[str] = []
    current: list[str] | None = None
    for line in existing.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("["):
            if current is not None:
                sections.append("\n".join(current).rstrip())
            header = stripped.lstrip("[")
            if header.startswith(("marketplaces.", "plugins.", "marketplaces]", "plugins]")):
                current = [line]
            else:
                current = None
        elif current is not None:
            current.append(line)
    if current is not None:
        sections.append("\n".join(current).rstrip())
    return "\n\n".join(section for section in sections if section)


def write_codex_config(
    codex_home: Path,
    mcp_servers: dict[str, dict[str, object]],
    *,
    gateway_base_url: str,
    model_settings: CodexModelSettings | None = None,
    hooks_enabled: bool = True,
) -> None:
    """Write the Pynchy-managed Codex CLI config for this per-group home."""
    codex_home.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / "config.toml"
    plugin_config = _preserved_plugin_config(config_path)
    settings = model_settings or CodexModelSettings()
    lines = [
        "# Generated by Pynchy for this sandbox. LLM auth is routed through LiteLLM.",
        'approval_policy = "never"',
        f"sandbox_mode = {_toml_value(DEFAULT_CODEX_SANDBOX_MODE)}",
        f"model_provider = {_toml_value(_PYNCHY_LITELLM_PROVIDER)}",
    ]
    if settings.model:
        lines.append(f"model = {_toml_value(settings.model)}")
    if settings.model_reasoning_effort:
        lines.append(f"model_reasoning_effort = {_toml_value(settings.model_reasoning_effort)}")
    lines.extend(
        [
            "",
            f"[model_providers.{_PYNCHY_LITELLM_PROVIDER}]",
            'name = "Pynchy LiteLLM Gateway"',
            f"base_url = {_toml_value(gateway_base_url)}",
            'wire_api = "responses"',
            'env_key = "OPENAI_API_KEY"',
            "",
            "[sandbox_workspace_write]",
            "network_access = true",
        ]
    )
    if hooks_enabled:
        lines.extend(
            [
                "",
                "[features]",
                "hooks = true",
                "",
                "[[hooks.PreToolUse]]",
                'matcher = "*"',
                "",
                "[[hooks.PreToolUse.hooks]]",
                'type = "command"',
                f"command = {_toml_value(f'{sys.executable} -m agent_runner.security.hook_entry')}",
                "timeout = 30",
                'statusMessage = "Checking Pynchy security policy"',
            ]
        )

    for name, spec in sorted(mcp_servers.items()):
        lines.extend(_mcp_server_lines(name, spec))

    if plugin_config:
        lines.extend(["", plugin_config])
    config_path.write_text("\n".join(lines) + "\n")
