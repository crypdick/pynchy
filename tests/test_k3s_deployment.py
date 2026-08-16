"""Checks for reproducible K3s deployment inputs."""

from pathlib import Path


def test_host_image_installs_locked_dependencies() -> None:
    dockerfile = Path("deploy/k3s/host.Dockerfile").read_text(encoding="utf-8")

    assert "uv sync --locked --no-dev --all-extras --no-editable" in dockerfile
    assert "uv pip install --system --no-cache-dir '.[all]'" not in dockerfile
