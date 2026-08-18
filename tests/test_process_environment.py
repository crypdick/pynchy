"""Safe host process environment selection."""

from __future__ import annotations

from pynchy.process_environment import filtered_process_environment


def test_process_environment_excludes_unselected_secrets_and_allows_explicit_values(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("PYNCHY_KUBERNETES_VAULT_PVC", "vault")
    monkeypatch.setenv("SECRET_TOKEN", "hidden")

    environment = filtered_process_environment({"DISPLAY_COLOR": "chosen", "PATH": "/override"})

    assert environment["PATH"] == "/override"
    assert environment["DISPLAY_COLOR"] == "chosen"
    assert environment["PYNCHY_KUBERNETES_VAULT_PVC"] == "vault"
    assert "SECRET_TOKEN" not in environment
