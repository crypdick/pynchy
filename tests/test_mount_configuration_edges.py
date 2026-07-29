"""Container mount composition boundary contracts."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pynchy.host.container_manager.mounts import build_container_args


def test_container_args_require_composed_mount_operations(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.host.container_manager.mounts._mount_operations", None)

    with pytest.raises(RuntimeError, match="container mount operations have not been configured"):
        build_container_args([], "container", memory_mb=256, image="image")


def test_docker_container_args_add_host_gateway_when_gateway_is_active() -> None:
    with (
        patch("pynchy.host.container_manager.mounts._configured_mount_operations") as operations,
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=object()),
    ):
        operations.return_value.runtime_name.return_value = "docker"
        args = build_container_args([], "container", memory_mb=256, image="image")

    assert "--add-host" in args
    assert "host.docker.internal:host-gateway" in args
