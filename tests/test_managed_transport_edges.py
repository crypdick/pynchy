"""Public safety checks for managed Git object stores."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.host.git_ops.managed_transport import managed_object_store_is_safe

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_managed_object_store_rejects_non_directory_paths(tmp_path: Path, kind: str) -> None:
    object_dir = tmp_path / "objects"
    if kind == "file":
        object_dir.write_text("not a store\n")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        object_dir.symlink_to(target, target_is_directory=True)

    assert managed_object_store_is_safe(object_dir if kind != "missing" else None) is False


@pytest.mark.parametrize("unsafe_entry", ["info-symlink", "info-file", "alternates"])
def test_managed_object_store_rejects_redirecting_metadata(
    tmp_path: Path, unsafe_entry: str
) -> None:
    object_dir = tmp_path / "objects"
    object_dir.mkdir()
    info_dir = object_dir / "info"
    if unsafe_entry == "info-symlink":
        target = tmp_path / "target-info"
        target.mkdir()
        info_dir.symlink_to(target, target_is_directory=True)
    elif unsafe_entry == "info-file":
        info_dir.write_text("not a directory\n")
    else:
        info_dir.mkdir()
        (info_dir / "alternates").write_text("/outside\n")

    assert managed_object_store_is_safe(object_dir) is False


def test_managed_object_store_accepts_regular_nested_objects(tmp_path: Path) -> None:
    object_dir = tmp_path / "objects"
    (object_dir / "aa").mkdir(parents=True)
    (object_dir / "aa" / "object").write_bytes(b"git object")

    assert managed_object_store_is_safe(object_dir) is True


def test_managed_object_store_rejects_nested_symlink(tmp_path: Path) -> None:
    object_dir = tmp_path / "objects"
    nested_dir = object_dir / "aa"
    nested_dir.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_text("outside\n")
    (nested_dir / "object").symlink_to(target)

    assert managed_object_store_is_safe(object_dir) is False


def test_managed_object_store_fails_closed_when_scanning_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    object_dir = tmp_path / "objects"
    object_dir.mkdir()
    monkeypatch.setattr(
        "pynchy.host.git_ops.managed_transport.os.scandir",
        lambda _path: (_ for _ in ()).throw(OSError("store unavailable")),
    )

    assert managed_object_store_is_safe(object_dir) is False
