"""Security audit storage configuration contracts."""

from __future__ import annotations

import pytest

from pynchy.host.container_manager.security import prune_security_audit, record_security_event


async def test_record_security_event_requires_configured_storage(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.host.container_manager.security.audit._store_security_audit", None)
    with pytest.raises(RuntimeError, match="security audit storage has not been configured"):
        await record_security_event("chat", "workspace", "tool", "denied")


async def test_prune_security_audit_requires_configured_storage(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.host.container_manager.security.audit._prune_security_audit", None)
    with pytest.raises(RuntimeError, match="security audit storage has not been configured"):
        await prune_security_audit()
