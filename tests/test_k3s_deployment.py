"""Checks for reproducible K3s deployment inputs."""

from pathlib import Path


def test_host_image_installs_locked_dependencies() -> None:
    dockerfile = Path("deploy/k3s/host.Dockerfile").read_text(encoding="utf-8")

    assert "uv sync --locked --no-dev --all-extras --no-editable" in dockerfile
    assert "uv pip install --system --no-cache-dir '.[all]'" not in dockerfile


def test_android_usb_bridge_is_unprivileged_and_k3s_local() -> None:
    service = Path("deploy/k3s/pynchy-adb.service").read_text(encoding="utf-8")
    manifest = Path("deploy/k3s/pynchy.yaml").read_text(encoding="utf-8")

    assert "User=pynchy-adb" in service
    assert "NoNewPrivileges=true" in service
    assert "localfilesystem:/run/pynchy-adb/adb.sock" in service
    assert "path: /run/pynchy-adb" in manifest
    assert "privileged: true" not in manifest


def test_k3s_backup_uses_logical_database_snapshots() -> None:
    script = Path("deploy/k3s/backup.sh").read_text(encoding="utf-8")

    assert ".backup '$partial/$database'" in script
    assert "litellm temporal temporal_visibility" in script
    assert "pg_dump -U" in script
    assert "pg_restore -l" in script
    assert "/var/lib/postgresql/data/.pynchy-backup-$timestamp" in script
