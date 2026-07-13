from __future__ import annotations

import hashlib
import io
import tarfile
from typing import TYPE_CHECKING

import pytest
from scripts import install_new_feature_dependencies as installer

if TYPE_CHECKING:
    from pathlib import Path


def _archive_with_temporal(payload: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("release/temporal")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def test_temporal_binary_reads_only_named_archive_member() -> None:
    assert installer._temporal_binary(_archive_with_temporal(b"binary")) == b"binary"


def test_temporal_binary_rejects_missing_binary() -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("README.md")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"docs"))
    with pytest.raises(installer.DependencyError, match="exactly one temporal binary"):
        installer._temporal_binary(output.getvalue())


def test_verify_digest_rejects_tampering() -> None:
    expected = hashlib.sha256(b"expected").hexdigest()
    with pytest.raises(installer.DependencyError, match="checksum mismatch"):
        installer._verify_digest(b"tampered", expected)


def test_install_temporal_writes_verified_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = _archive_with_temporal(b"binary")
    monkeypatch.setattr(installer, "_platform_key", lambda: ("linux", "amd64"))
    monkeypatch.setattr(installer, "_download", lambda _url: archive)
    monkeypatch.setitem(
        installer._TEMPORAL_DIGESTS,
        ("linux", "amd64"),
        hashlib.sha256(archive).hexdigest(),
    )

    destination = installer._install_temporal(tmp_path / "bin")

    assert destination.read_bytes() == b"binary"
    assert destination.stat().st_mode & 0o111 == 0o111


def test_runtime_dependencies_install_temporal_in_the_selected_bin_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "runtime-bin"
    installed: list[Path] = []
    temporal = bin_dir / "temporal"

    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(installer, "_docker_ready", lambda _docker: True)

    def install(destination: Path) -> Path:
        installed.append(destination)
        destination.mkdir()
        temporal.touch()
        return temporal

    monkeypatch.setattr(installer, "_install_temporal", install)

    installer._ensure_runtime_dependencies(bin_dir=bin_dir, check_only=False)

    assert installed == [bin_dir]


def test_runtime_dependencies_reject_missing_pinned_temporal_in_check_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(installer, "_docker_ready", lambda _docker: True)

    with pytest.raises(installer.DependencyError, match=r"Pinned Temporal CLI v1\.8\.0 is missing"):
        installer._ensure_runtime_dependencies(bin_dir=tmp_path / "runtime-bin", check_only=True)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", ("linux", "amd64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Darwin", "x86_64", ("darwin", "amd64")),
        ("Darwin", "arm64", ("darwin", "arm64")),
    ],
)
def test_platform_key_normalizes_supported_hosts(monkeypatch, system, machine, expected) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    assert installer._platform_key() == expected
