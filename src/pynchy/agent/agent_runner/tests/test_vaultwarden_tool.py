"""Vaultwarden tool visibility follows resolved channel grants."""

from agent_runner.agent_tools import enabled_agent_tools


def test_get_secret_visible_only_with_vaultwarden_grant() -> None:
    assert "get_secret" not in enabled_agent_tools([])
    assert "get_secret" in enabled_agent_tools(["vaultwarden"])


def test_vaultwarden_admin_visible_only_with_explicit_grant() -> None:
    assert "manage_vaultwarden" not in enabled_agent_tools(["vaultwarden"])
    assert "manage_vaultwarden" in enabled_agent_tools(["vaultwarden-admin"])
