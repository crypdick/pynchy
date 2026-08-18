"""Tool-owned credential and companion-skill authorization."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pynchy.config.api import (
    apply_tool_access,
    resolve_tool_access,
    tool_process_environment,
    validate_settings_mapping,
)
from pynchy.host.container_manager.credentials import (
    build_agent_env_vars,
    configure_workspace_environment,
)
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspaceRestriction,
    clear_runtime_workspace_restrictions,
    load_resolved_config,
    load_resolved_tool_access,
    register_runtime_workspace_restriction,
)

_GITHUB_SECRET = "github-secret"  # noqa: S105  # pragma: allowlist secret
_LINEAR_SECRET = "linear-secret"  # noqa: S105  # pragma: allowlist secret
_PROTON_SECRET = "proton-secret"  # noqa: S105  # pragma: allowlist secret
_UNRELATED_SECRET = "must-not-leak"  # noqa: S105  # pragma: allowlist secret
_REDACTED_SECRET = "do-not-render"  # noqa: S105  # pragma: allowlist secret


def _settings():
    return validate_settings_mapping(
        {
            "tools": {
                "github-cli": {
                    "type": "workspace",
                    "skills": ["github-auth"],
                    "required_env": ["GITHUB_TOKEN"],
                },
                "linear": {
                    "type": "linear",
                    "skills": ["linear"],
                    "required_env": ["LINEAR_SYNAPSE_API_KEY"],
                    "optional_env": ["LINEAR_SYNAPSE_TEAM_KEY"],
                    "expose_env_to_workspace": True,
                },
                "proton-mail": {
                    "type": "mcp",
                    "skills": ["reading-proton-email"],
                    "required_env": ["PROTON_PASSWORD"],
                    "mcp": {
                        "runtime": "script",
                        "command": "uv",
                        "args": ["run", "proton"],
                        "port": 8475,
                    },
                },
            },
            "profiles": {
                "dev": {
                    "tools": ["github-cli", "linear", "proton-mail"],
                    "skills": ["linear", "reading-proton-email", "ordinary"],
                },
                "standalone": {"skills": ["github-auth", "reading-proton-email"]},
            },
            "workspaces": {
                "dev": {"profiles": ["dev"]},
                "standalone": {"profiles": ["standalone"]},
            },
        }
    )


def test_selected_tools_install_companions_and_expose_only_workspace_grants() -> None:
    settings = _settings()
    resolved = settings.resolved_workspace_config("dev")
    assert resolved is not None
    environ = {
        "GITHUB_TOKEN": _GITHUB_SECRET,
        "LINEAR_SYNAPSE_API_KEY": _LINEAR_SECRET,
        "PROTON_PASSWORD": _PROTON_SECRET,
        "UNRELATED_HOST_SECRET": _UNRELATED_SECRET,
    }

    effective, access = apply_tool_access(settings.tools, resolved, environ=environ)

    assert effective.tools == ["github-cli", "linear", "proton-mail"]
    assert effective.skills == [
        "ordinary",
        "github-auth",
        "linear",
        "reading-proton-email",
    ]
    assert access.workspace_env == {
        "GITHUB_TOKEN": _GITHUB_SECRET,
        "LINEAR_SYNAPSE_API_KEY": _LINEAR_SECRET,
    }
    assert "UNRELATED_HOST_SECRET" not in access.workspace_env
    assert effective.contains_secrets is True


def test_disabled_tool_does_not_receive_a_credential_grant() -> None:
    settings = _settings()
    resolved = settings.resolved_workspace_config("dev")
    assert resolved is not None
    disabled_linear = settings.tools["linear"].model_copy(update={"enabled": False})

    access = resolve_tool_access(
        {"linear": disabled_linear},
        replace(resolved, tools=["linear"]),
        environ={"LINEAR_SYNAPSE_API_KEY": _LINEAR_SECRET},
    )

    assert access.tools == ()
    assert access.companion_skills == ()
    assert access.workspace_env == {}
    assert access.missing_requirements == {}


def test_agent_tool_grants_include_canonical_type_for_custom_tool_name() -> None:
    settings = _settings()
    resolved = settings.resolved_workspace_config("dev")
    assert resolved is not None

    access = resolve_tool_access(
        {"linear-synapse": settings.tools["linear"]},
        replace(resolved, tools=["linear-synapse"]),
        environ={"LINEAR_SYNAPSE_API_KEY": _LINEAR_SECRET},
    )

    assert access.agent_tool_grants == ("linear-synapse", "linear")


def test_agent_process_receives_only_selected_workspace_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_SECRET)
    monkeypatch.setenv("LINEAR_SYNAPSE_API_KEY", _LINEAR_SECRET)
    monkeypatch.setenv("PROTON_PASSWORD", _PROTON_SECRET)
    monkeypatch.setenv("UNRELATED_HOST_SECRET", _UNRELATED_SECRET)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.workspace_config.get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "pynchy.host.container_manager.gateway.get_gateway",
        lambda: None,
    )
    monkeypatch.setattr(
        "pynchy.host.container_manager.credentials._read_git_identity",
        lambda: (None, None),
    )
    configure_workspace_environment(
        lambda *, is_admin, group_folder: dict(
            access.workspace_env
            if (access := load_resolved_tool_access(group_folder)) is not None
            else {}
        )
    )

    environment = build_agent_env_vars(is_admin=False, group_folder="dev")

    assert environment["GITHUB_TOKEN"] == _GITHUB_SECRET
    assert environment["LINEAR_SYNAPSE_API_KEY"] == _LINEAR_SECRET
    assert "PROTON_PASSWORD" not in environment
    assert "UNRELATED_HOST_SECRET" not in environment


def test_agent_process_receives_openai_gateway_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OpenAIGateway:
        base_url = "http://gateway.example:4000"
        key = "gateway-key"  # pragma: allowlist secret

        def has_provider(self, provider: str) -> bool:
            return provider == "openai"

    monkeypatch.setattr("pynchy.host.container_manager.gateway.get_gateway", _OpenAIGateway)
    monkeypatch.setattr(
        "pynchy.host.container_manager.credentials._read_git_identity", lambda: (None, None)
    )
    configure_workspace_environment(
        lambda *, is_admin, group_folder: {"WORKSPACE": f"{group_folder}:{is_admin}"}
    )

    environment = build_agent_env_vars(is_admin=True, group_folder="assigned")

    assert environment["OPENAI_BASE_URL"] == "http://gateway.example:4000"
    assert environment["OPENAI_API_KEY"] == "gateway-key"  # pragma: allowlist secret
    assert "ANTHROPIC_BASE_URL" not in environment
    assert environment["NO_PROXY"].endswith("gateway.example")
    assert environment["WORKSPACE"] == "assigned:True"


def test_gateway_without_a_hostname_keeps_no_proxy_hosts_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HostnameLessGateway:
        base_url = "http://"
        key = "gateway-key"  # pragma: allowlist secret

        def has_provider(self, provider: str) -> bool:
            return provider == "openai"

    monkeypatch.setattr("pynchy.host.container_manager.gateway.get_gateway", _HostnameLessGateway)
    monkeypatch.setattr(
        "pynchy.host.container_manager.credentials._read_git_identity", lambda: (None, None)
    )
    configure_workspace_environment(lambda **_kwargs: {})

    environment = build_agent_env_vars(is_admin=False, group_folder="assigned")

    assert environment["NO_PROXY"] == "localhost,127.0.0.1,::1,host.docker.internal"


def test_agent_environment_requires_composition_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pynchy.host.container_manager.gateway.get_gateway", lambda: None)
    monkeypatch.setattr("pynchy.host.container_manager.credentials._workspace_env_vars", None)
    monkeypatch.setattr(
        "pynchy.host.container_manager.credentials._read_git_identity", lambda: (None, None)
    )

    with pytest.raises(RuntimeError, match="workspace environment has not been configured"):
        build_agent_env_vars(is_admin=False, group_folder="assigned")


def test_missing_required_environment_disables_only_affected_tool_value_free() -> None:
    settings = _settings()
    resolved = settings.resolved_workspace_config("dev")
    assert resolved is not None

    effective, access = apply_tool_access(
        settings.tools,
        resolved,
        environ={"GITHUB_TOKEN": _REDACTED_SECRET},
    )

    assert effective.tools == ["github-cli"]
    assert effective.skills == ["ordinary", "github-auth"]
    assert access.missing_requirements == {
        "linear": ("LINEAR_SYNAPSE_API_KEY",),
        "proton-mail": ("PROTON_PASSWORD",),
    }
    notice = "\n".join(access.notices)
    assert "LINEAR_SYNAPSE_API_KEY" in notice
    assert "PROTON_PASSWORD" in notice
    assert _REDACTED_SECRET not in notice


def test_profile_and_learned_skill_names_cannot_grant_companion_access() -> None:
    settings = _settings()
    resolved = settings.resolved_workspace_config("standalone")
    assert resolved is not None

    effective, access = apply_tool_access(
        settings.tools,
        resolved,
        environ={
            "GITHUB_TOKEN": _GITHUB_SECRET,
            "PROTON_PASSWORD": _PROTON_SECRET,
        },
    )

    assert effective.tools == []
    assert effective.skills == []
    assert access.workspace_env == {}


def test_runtime_implementation_without_toml_tool_declaration_is_not_authorized() -> None:
    settings = _settings()
    resolved = settings.resolved_workspace_config("standalone")
    assert resolved is not None

    effective, access = apply_tool_access(
        settings.tools,
        replace(resolved, tools=["plugin-only"]),
        environ={},
    )

    assert effective.tools == []
    assert access.tools == ()


def test_linear_runtime_maps_configured_source_names_to_provider_names() -> None:
    settings = _settings()
    tool = settings.tools["linear"]

    environment = tool_process_environment(
        tool,
        environ={
            "LINEAR_SYNAPSE_API_KEY": _LINEAR_SECRET,
            "LINEAR_SYNAPSE_TEAM_KEY": "SYN",
        },
    )

    assert environment == {
        "LINEAR_API_KEY": _LINEAR_SECRET,
        "LINEAR_TEAM_KEY": "SYN",
    }


def test_route_restriction_precedes_companion_and_environment_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: settings)
    register_runtime_workspace_restriction(
        "dev-child",
        RuntimeWorkspaceRestriction(parent_workspace="dev", tools=()),
    )
    monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_SECRET)
    monkeypatch.setenv("LINEAR_SYNAPSE_API_KEY", _LINEAR_SECRET)
    monkeypatch.setenv("PROTON_PASSWORD", _PROTON_SECRET)

    try:
        effective = load_resolved_config("dev-child")
        access = load_resolved_tool_access("dev-child")
    finally:
        clear_runtime_workspace_restrictions()

    assert effective is not None
    assert access is not None
    assert effective.tools == []
    assert effective.skills == ["ordinary"]
    assert access.workspace_env == {}


def test_optional_environment_does_not_control_availability() -> None:
    settings = _settings()
    resolved = settings.resolved_workspace_config("dev")
    assert resolved is not None

    access = resolve_tool_access(
        settings.tools,
        resolved,
        environ={
            "GITHUB_TOKEN": _GITHUB_SECRET,
            "LINEAR_SYNAPSE_API_KEY": _LINEAR_SECRET,
            "PROTON_PASSWORD": _PROTON_SECRET,
        },
    )

    assert "linear" in access.tools
    assert "LINEAR_SYNAPSE_TEAM_KEY" not in access.workspace_env
    assert tool_process_environment(
        settings.tools["linear"],
        environ={
            "LINEAR_SYNAPSE_API_KEY": _LINEAR_SECRET,
        },
    ) == {"LINEAR_API_KEY": _LINEAR_SECRET}
