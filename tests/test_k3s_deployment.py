"""Checks for reproducible K3s deployment inputs."""

import os
import sqlite3
import subprocess  # noqa: S404 - test executes repository and system binaries with fixed arguments.
from pathlib import Path

import yaml


def _documents(path: str) -> list[dict]:
    return list(yaml.safe_load_all(Path(path).read_text(encoding="utf-8")))


def test_k3s_base_leaves_local_storage_to_deployment_overlay() -> None:
    kustomization = Path("deploy/k3s/kustomization.yaml").read_text(encoding="utf-8")

    assert Path("deploy/k3s/storage.example.yaml").is_file()
    assert "storage.example.yaml" not in kustomization


def test_host_image_installs_locked_dependencies() -> None:
    dockerfile = Path("deploy/k3s/host.Dockerfile").read_text(encoding="utf-8")

    assert "chromium-sandbox" not in dockerfile
    assert "uv sync --locked --no-dev --all-extras --no-editable" in dockerfile
    assert '.venv/bin/python -c "import caldav, detect_secrets, requests"' in dockerfile
    assert "uv pip install --system --no-cache-dir '.[all]'" not in dockerfile
    assert "ARG PYNCHY_RELEASE_SHA" in dockerfile
    assert "PYNCHY_RELEASE_SHA=${PYNCHY_RELEASE_SHA}" in dockerfile
    assert "npm install -g @playwright/mcp@0.0.79" in dockerfile
    assert "    xauth \\\n" in dockerfile
    assert "scrcpy-server-v4.0" in dockerfile
    scrcpy_checksum = (
        "84924bd564a1eb6089c872c7521f9680"  # pragma: allowlist secret
        "58977f91f5ff02514a8c74aff3210f3a"  # pragma: allowlist secret
    )
    assert scrcpy_checksum in dockerfile
    assert "cli-v2026.7.0/bw-linux-2026.7.0.zip" in dockerfile
    checksum = (
        "7a35145e205952f7434d2370da359543"  # pragma: allowlist secret
        "145ae0c45ba1af0fe9bdd99d40a00180"  # pragma: allowlist secret
    )
    assert checksum in dockerfile


def test_vaultwarden_is_pinned_and_uses_external_retained_storage() -> None:
    documents = _documents("deploy/k3s/application/vaultwarden.yaml")
    stateful_set = next(document for document in documents if document["kind"] == "StatefulSet")
    service = next(document for document in documents if document["kind"] == "Service")
    pod = stateful_set["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert container["image"] == (
        "vaultwarden/server:1.37.1@"
        "sha256:ebdfe70701c60ac0c28c697e787cea767d7972940b786037b29fe0d507f821e8"
    )
    assert pod["automountServiceAccountToken"] is False
    assert container["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "pynchy-vaultwarden",
        "key": "DOMAIN",
    }
    assert container["env"][1] == {
        "name": "ROCKET_TLS",
        "value": '{certs="/tls/tls.crt",key="/tls/tls.key"}',
    }
    assert container["ports"] == [{"name": "https", "containerPort": 8080}]
    assert container["startupProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert container["livenessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "pynchy-vaultwarden"
    assert pod["volumes"][1]["secret"]["secretName"] == (
        "pynchy-vaultwarden-tls"  # pragma: allowlist secret
    )
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    assert service["spec"]["ports"] == [{"name": "https", "port": 443, "targetPort": "https"}]
    assert "pynchy-vaultwarden-local" in Path("deploy/k3s/storage.example.yaml").read_text(
        encoding="utf-8"
    )


def test_vaultwarden_ingress_is_limited_to_its_pods() -> None:
    policies = _documents("deploy/k3s/application/network-policy.yaml")
    internal = next(item for item in policies if item["metadata"]["name"] == "pynchy-ingress")
    web = next(item for item in policies if item["metadata"]["name"] == "pynchy-web-ingress")
    policy = next(
        item for item in policies if item["metadata"]["name"] == "pynchy-vaultwarden-ingress"
    )

    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "pynchy-vaultwarden"}
    assert internal["spec"]["podSelector"] == {
        "matchExpressions": [{"key": "app", "operator": "NotIn", "values": ["pynchy-vaultwarden"]}]
    }
    assert web["spec"]["podSelector"]["matchLabels"] == {"app": "pynchy"}
    assert policy["spec"]["ingress"] == [
        {
            "from": [{"podSelector": {"matchLabels": {"app": "pynchy"}}}],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }
    ]


def test_agent_image_installs_locked_dependencies() -> None:
    dockerfile = Path("src/pynchy/agent/Dockerfile").read_text(encoding="utf-8")

    assert "uv export --frozen --no-dev --no-emit-project" in dockerfile
    assert "uv pip install --system --no-cache-dir --no-deps" in dockerfile
    assert "uv pip install --system --no-cache-dir /opt/pynchy/agent-runner" not in dockerfile


def test_channel_browser_uses_persistent_profile_and_managed_policy() -> None:
    script = Path("deploy/k3s/vaultwarden-browser.sh").read_text(encoding="utf-8")
    deployment = _documents("deploy/k3s/application/pynchy.yaml")[0]
    pod = deployment["spec"]["template"]["spec"]
    host = pod["containers"][0]

    assert "--shared-browser-context" in script
    assert '--user-data-dir "$profile"' in script
    assert "--no-sandbox" in script
    assert "-displayfd 3" in script
    assert '"--disable-background-networking"' in script
    assert '"--disable-component-update"' in script
    assert '"--disable-extensions"' in script
    assert "SingletonLock SingletonSocket SingletonCookie" in script
    assert "xvfb-run" not in script
    assert {
        "name": "bitwarden-policy",
        "mountPath": "/etc/chromium/policies/managed",
        "readOnly": True,
    } in host["volumeMounts"]
    assert {
        "name": "vaultwarden-ca",
        "mountPath": "/etc/pynchy-vaultwarden",
        "readOnly": True,
    } in host["volumeMounts"]
    policy_volume = next(
        volume for volume in pod["volumes"] if volume["name"] == "bitwarden-policy"
    )
    assert policy_volume["configMap"]["optional"] is True
    ca_volume = next(volume for volume in pod["volumes"] if volume["name"] == "vaultwarden-ca")
    assert ca_volume["configMap"] == {
        "name": "pynchy-vaultwarden-ca",
        "optional": True,
    }


def test_pynchy_startup_probe_allows_workspace_recovery() -> None:
    deployment = _documents("deploy/k3s/application/pynchy.yaml")[0]
    container = next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == "pynchy"
    )

    assert container["startupProbe"] == {
        "tcpSocket": {"port": "web"},
        "failureThreshold": 60,
        "periodSeconds": 5,
    }
    assert container["readinessProbe"] == {
        "httpGet": {"path": "/health", "port": "web"},
        "periodSeconds": 5,
    }


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
    assert "-c 'import agent_runner.agent_tools'" in workflow


def test_k3s_release_monitor_has_narrow_namespace_permissions() -> None:
    workload = Path("deploy/k3s/application/release-monitor.yaml").read_text(encoding="utf-8")
    permissions = Path("deploy/k3s/bootstrap/release-monitor-rbac.yaml").read_text(encoding="utf-8")

    assert "kind: CronJob" in workload
    assert "suspend: false" in workload
    assert "concurrencyPolicy: Forbid" in workload
    assert 'resources: ["deployments", "statefulsets"]' in permissions
    assert 'resources: ["pods"]' in permissions
    assert 'verbs: ["get", "list", "watch", "create", "delete"]' in permissions
    assert 'resources: ["secrets"]' not in permissions
    assert "ClusterRole" not in permissions
    assert "hostPath:" not in workload
    assert "docker.sock" not in workload
    assert "claimName: pynchy-data" in workload


def test_linux_desktop_is_isolated_and_persistent() -> None:
    documents = _documents("deploy/k3s/application/desktop.yaml")
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert deployment["metadata"]["name"] == "pynchy-desktop"
    assert pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod
    assert pod["imagePullSecrets"] == [{"name": "pynchy-ghcr"}]
    assert "envFrom" not in container
    assert "ports" not in container
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["appArmorProfile"] == {
        "type": "Localhost",
        "localhostProfile": "pynchy-chromium",
    }
    assert container["securityContext"]["seccompProfile"] == {"type": "Unconfined"}
    assert Path("deploy/k3s/pynchy-chromium.apparmor").is_file()
    assert container["startupProbe"]["timeoutSeconds"] == 5
    assert container["livenessProbe"]["timeoutSeconds"] == 5
    assert container["image"].startswith("pynchy-host:")
    assert container["volumeMounts"] == [
        {"name": "browser-profile", "mountPath": "/home/pynchy/.config/chromium"}
    ]
    assert pod["volumes"] == [
        {"name": "browser-profile", "persistentVolumeClaim": {"claimName": "pynchy-desktop"}}
    ]


def test_linux_desktop_removes_retired_pod_browser_locks_before_startup() -> None:
    entrypoint = Path("deploy/k3s/desktop-entrypoint.sh").read_text(encoding="utf-8")

    cleanup = 'rm -f "$profile/$name"'
    assert cleanup in entrypoint
    assert entrypoint.index(cleanup) < entrypoint.index('Xvfb "$display"')


def test_linux_desktop_waits_for_x_display_before_openbox() -> None:
    entrypoint = Path("deploy/k3s/desktop-entrypoint.sh").read_text(encoding="utf-8")

    wait_for_display = "while ! xdotool getmouselocation"
    assert wait_for_display in entrypoint
    assert entrypoint.index('Xvfb "$display"') < entrypoint.index(wait_for_display)
    assert entrypoint.index(wait_for_display) < entrypoint.index("openbox &")


def test_runtime_can_exec_only_namespace_pods() -> None:
    manifest = Path("deploy/k3s/bootstrap/rbac.yaml").read_text(encoding="utf-8")

    assert 'resources: ["deployments"]' in manifest
    assert 'verbs: ["get"]' in manifest
    assert 'resources: ["pods/exec"]' in manifest
    assert "ClusterRole" not in manifest


def test_android_usb_bridge_is_unprivileged_and_k3s_local() -> None:
    service = Path("deploy/k3s/pynchy-adb.service").read_text(encoding="utf-8")
    manifest = Path("deploy/k3s/application/pynchy.yaml").read_text(encoding="utf-8")

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
    assert '"$PYNCHY_K3S_STORAGE_ROOT/vaultwarden"' in script
    assert '"$PYNCHY_K3S_STORAGE_ROOT/backups"' in script
    assert ".backup '$partial/$database'" in script
    assert "litellm temporal temporal_visibility" in script
    assert "pg_dump -U" in script
    assert "pg_restore -l" in script
    assert "/var/lib/postgresql/data/.pynchy-backup-$timestamp" in script
    assert '"$vaultwarden_volume/db.sqlite3"' in script


def test_k3s_backup_checksums_nested_vaultwarden_files(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    for directory in ("shared/app/data", "postgres", "vaultwarden"):
        (storage / directory).mkdir(parents=True)
    for database in ("messages.db", "neonize.db"):
        with sqlite3.connect(storage / "shared/app/data" / database) as connection:
            connection.execute("CREATE TABLE canary (value TEXT)")
    with sqlite3.connect(storage / "vaultwarden/db.sqlite3") as connection:
        connection.execute("CREATE TABLE canary (value TEXT)")
    (storage / "vaultwarden/SHA256SUMS").write_text("attachment", encoding="utf-8")
    timestamp = "20000101T000000Z"
    postgres_staging = storage / f"postgres/.pynchy-backup-{timestamp}"
    postgres_staging.mkdir()
    for database in ("litellm", "temporal", "temporal_visibility"):
        (postgres_staging / f"{database}.dump").write_text("dump", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_k3s = fake_bin / "k3s"
    fake_k3s.write_text(
        """#!/bin/sh
case " $* " in
  *" get secret pynchy-runtime "*) printf 'dGVzdA==' ;;
esac
""",
        encoding="utf-8",
    )
    fake_k3s.chmod(0o755)
    for command, body in (("chown", "exit 0"), ("date", f"printf {timestamp}")):
        executable = fake_bin / command
        executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        executable.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYNCHY_K3S_STORAGE_ROOT": str(storage),
    }
    result = subprocess.run(
        ["/usr/bin/bash", "deploy/k3s/backup.sh"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    snapshot = storage / "backups" / timestamp
    assert (snapshot / "vaultwarden/db.sqlite3").is_file()
    verified = subprocess.run(
        ["/usr/bin/sha256sum", "-c", "SHA256SUMS"],
        cwd=snapshot,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert "./vaultwarden/SHA256SUMS: OK" in verified.stdout
