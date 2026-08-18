"""Checks for reproducible K3s deployment inputs."""

from pathlib import Path


def test_k3s_base_leaves_local_storage_to_deployment_overlay() -> None:
    kustomization = Path("deploy/k3s/kustomization.yaml").read_text(encoding="utf-8")

    assert Path("deploy/k3s/storage.example.yaml").is_file()
    assert "storage.example.yaml" not in kustomization


def test_host_image_installs_locked_dependencies() -> None:
    dockerfile = Path("deploy/k3s/host.Dockerfile").read_text(encoding="utf-8")

    assert "uv sync --locked --no-dev --all-extras --no-editable" in dockerfile
    assert "uv pip install --system --no-cache-dir '.[all]'" not in dockerfile
    assert "ARG PYNCHY_RELEASE_SHA" in dockerfile
    assert "PYNCHY_RELEASE_SHA=${PYNCHY_RELEASE_SHA}" in dockerfile


def test_main_workflow_publishes_both_images_after_tests() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "needs: [test, runtime]" in workflow
    assert "packages: write" in workflow
    assert "ghcr.io/$owner/pynchy-host:$RELEASE_SHA" in workflow
    assert "ghcr.io/$owner/pynchy-agent:$RELEASE_SHA" in workflow
    assert workflow.index("touch src/pynchy/agent/requirements-plugins.txt") < workflow.index(
        "--build-arg AGENT_UID=3000"
    )
    assert workflow.index("docker run --rm") < workflow.index('docker push "$host_image"')


def test_k3s_release_monitor_has_narrow_namespace_permissions() -> None:
    manifest = Path("deploy/k3s/release-monitor.yaml").read_text(encoding="utf-8")

    assert "kind: CronJob" in manifest
    assert "concurrencyPolicy: Forbid" in manifest
    assert 'resources: ["deployments"]' in manifest
    assert 'resources: ["pods"]' in manifest
    assert 'verbs: ["get", "list", "watch", "create", "delete"]' in manifest
    assert 'resources: ["secrets"]' not in manifest
    assert "ClusterRole" not in manifest
    assert "hostPath:" not in manifest
    assert "docker.sock" not in manifest


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

    assert "PYNCHY_K3S_STORAGE_ROOT:?" in script
    assert '"$PYNCHY_K3S_STORAGE_ROOT/shared/app/data"' in script
    assert '"$PYNCHY_K3S_STORAGE_ROOT/postgres"' in script
    assert '"$PYNCHY_K3S_STORAGE_ROOT/backups"' in script
    assert ".backup '$partial/$database'" in script
    assert "litellm temporal temporal_visibility" in script
    assert "pg_dump -U" in script
    assert "pg_restore -l" in script
    assert "/var/lib/postgresql/data/.pynchy-backup-$timestamp" in script
