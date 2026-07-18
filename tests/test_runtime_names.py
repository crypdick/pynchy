from __future__ import annotations

import pytest

from pynchy.host.container_manager.orchestrator import (
    oneshot_container_name,
    stable_container_name,
)
from pynchy.host.container_manager.runtime_names import (
    runtime_container_name,
    runtime_namespace,
    runtime_network_name,
    runtime_volume_name,
)


def test_runtime_names_keep_production_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYNCHY_RUNTIME_NAMESPACE", raising=False)
    assert runtime_namespace() == "pynchy"
    assert runtime_container_name("litellm") == "pynchy-litellm"
    assert runtime_network_name("litellm-net") == "pynchy-litellm-net"
    assert runtime_volume_name("litellm-db-data") == "pynchy-litellm-db-data"


def test_runtime_names_scope_feature_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy-example-a1b2")
    assert runtime_container_name("litellm") == "pynchy-example-a1b2-litellm"
    assert runtime_network_name("litellm-net") == "pynchy-example-a1b2-litellm-net"
    assert runtime_volume_name("litellm-db-data") == "pynchy-example-a1b2-litellm-db-data"
    assert stable_container_name("admin/one") == "pynchy-example-a1b2-admin-one"
    assert oneshot_container_name("jobs").startswith("pynchy-example-a1b2-jobs-")


def test_runtime_namespace_rejects_unsafe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "Pynchy/../../prod")
    with pytest.raises(ValueError, match="PYNCHY_RUNTIME_NAMESPACE"):
        runtime_namespace()
