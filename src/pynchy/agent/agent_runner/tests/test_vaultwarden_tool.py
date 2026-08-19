"""Vaultwarden tool visibility follows resolved channel grants."""

from agent_runner.agent_tools import enabled_agent_tools


def test_get_secret_visible_only_with_vaultwarden_grant() -> None:
    assert "get_secret" not in enabled_agent_tools([])
    assert "get_secret" in enabled_agent_tools(["vaultwarden"])
