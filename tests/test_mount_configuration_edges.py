"""Container mount composition boundary contracts."""

from __future__ import annotations

import pytest

from pynchy.host.container_manager.mounts import build_container_args


def test_container_args_require_composed_mount_operations(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.host.container_manager.mounts._mount_operations", None)

    with pytest.raises(RuntimeError, match="container mount operations have not been configured"):
        build_container_args([], "container", memory_mb=256, image="image")
