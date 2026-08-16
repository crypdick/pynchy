"""Translate Pynchy container commands into namespace-scoped Kubernetes Pods."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess  # noqa: S404 - adapter executes fixed kubectl argv without a shell.
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SERVICE_ACCOUNT_ROOT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_KUBECONFIG_PATH = Path(os.environ.get("PYNCHY_KUBECONFIG", "/run/pynchy/kubeconfig.json"))
_DNS_NAME = re.compile(r"[^a-z0-9-]+")
_MANAGED_LABEL = "app.kubernetes.io/managed-by"
_NAME_HASH_LABEL = "pynchy.dev/runtime-name-hash"
_NAME_ANNOTATION = "pynchy.dev/runtime-name"


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    namespace: str
    pvc_name: str
    shared_root: Path
    pull_policy: str


@dataclass(slots=True)
class _RunRequest:
    name: str = ""
    image: str = ""
    command: list[str] = field(default_factory=list)
    detached: bool = False
    memory: str | None = None
    environment: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    mounts: list[tuple[str, str, bool]] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)


def runtime_settings() -> RuntimeSettings:
    return RuntimeSettings(
        namespace=os.environ.get("PYNCHY_KUBERNETES_NAMESPACE", "pynchy"),
        pvc_name=os.environ.get("PYNCHY_KUBERNETES_PVC", "pynchy-data"),
        shared_root=Path(os.environ.get("PYNCHY_KUBERNETES_SHARED_ROOT", "/srv/pynchy")),
        pull_policy=os.environ.get("PYNCHY_KUBERNETES_PULL_POLICY", "IfNotPresent"),
    )


def pod_name(runtime_name: str) -> str:
    """Return stable DNS label for one runtime container name."""
    normalized = _DNS_NAME.sub("-", runtime_name.lower()).strip("-") or "pynchy"
    if len(normalized) <= 63:
        return normalized
    digest = hashlib.sha256(runtime_name.encode()).hexdigest()[:10]
    return f"{normalized[:52].rstrip('-')}-{digest}"


def _name_hash(runtime_name: str) -> str:
    return hashlib.sha256(runtime_name.encode()).hexdigest()[:16]


def _parse_run(args: list[str]) -> _RunRequest:
    request = _RunRequest()
    index = 1
    while index < len(args):
        argument = args[index]
        if request.image:
            request.command.extend(args[index:])
            break
        if argument == "-d":
            request.detached = True
            index += 1
            continue
        if argument == "--init":
            index += 1
            continue
        if argument in {"--network", "--restart", "--add-host"}:
            index += 2
            continue
        if argument in {"--name", "--memory", "--label", "-e", "-v", "-p", "--mount"}:
            if index + 1 >= len(args):
                raise ValueError(f"Missing value for {argument}")
            value = args[index + 1]
            _apply_run_option(request, argument, value)
            index += 2
            continue
        if argument.startswith("-"):
            raise ValueError(f"Unsupported container run option: {argument}")
        request.image = argument
        index += 1
    if not request.name or not request.image:
        raise ValueError("Container run requires --name and image")
    return request


def _apply_run_option(request: _RunRequest, option: str, value: str) -> None:
    if option == "--name":
        request.name = value
    elif option == "--memory":
        request.memory = value
    elif option == "--label":
        key, separator, label_value = value.partition("=")
        if separator:
            request.labels[key] = label_value
    elif option == "-e":
        request.environment.append(value)
    elif option == "-v":
        request.mounts.append(_parse_volume(value))
    elif option == "--mount":
        request.mounts.append(_parse_mount(value))
    elif option == "-p":
        request.ports.append(_container_port(value))


def _parse_volume(value: str) -> tuple[str, str, bool]:
    source, separator, remainder = value.partition(":")
    if not separator:
        raise ValueError(f"Invalid volume: {value}")
    target, _separator, mode = remainder.partition(":")
    return source, target, mode == "ro"


def _parse_mount(value: str) -> tuple[str, str, bool]:
    values: dict[str, str] = {}
    readonly = False
    for item in value.split(","):
        key, separator, field_value = item.partition("=")
        if separator:
            values[key] = field_value
        elif key == "readonly":
            readonly = True
    if values.get("type") != "bind" or not values.get("source") or not values.get("target"):
        raise ValueError(f"Unsupported mount: {value}")
    return values["source"], values["target"], readonly


def _container_port(value: str) -> int:
    raw_port = value.rsplit(":", maxsplit=1)[-1].split("/", maxsplit=1)[0]
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid container port: {value}")
    return port


def _memory_quantity(value: str) -> str:
    if value.endswith("m") and value[:-1].isdigit():
        return f"{value[:-1]}Mi"
    return value


def _mount_spec(
    source: str,
    target: str,
    *,
    readonly: bool,
    shared_root: Path,
) -> dict[str, object]:
    if "/" not in source and not source.startswith("."):
        source_path = shared_root / ".runtime" / "volumes" / source
        source_path.mkdir(parents=True, exist_ok=True)
    else:
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = shared_root / source_path
    root = shared_root.resolve()
    resolved = source_path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Mount source is outside Kubernetes shared root: {source}") from exc
    if not readonly and resolved.is_dir():
        mode = stat.S_IMODE(resolved.stat().st_mode)
        resolved.chmod(mode | stat.S_ISGID | stat.S_IWGRP | stat.S_IXGRP)
    mount: dict[str, object] = {"name": "shared", "mountPath": target}
    if relative.parts:
        mount["subPath"] = relative.as_posix()
    if readonly:
        mount["readOnly"] = True
    return mount


def build_resources(
    args: list[str],
    *,
    shared_root: Path,
    pvc_name: str,
    namespace: str,
    pull_policy: str = "IfNotPresent",
) -> list[dict[str, Any]]:
    """Translate the run subset emitted by Pynchy into Pod and Service resources."""
    request = _parse_run(args)
    name = pod_name(request.name)
    selector = {_NAME_HASH_LABEL: _name_hash(request.name)}
    labels = {_MANAGED_LABEL: "pynchy", **selector, **request.labels}
    environment: list[dict[str, str]] = []
    for declared in request.environment:
        key, separator, inline_value = declared.partition("=")
        value = inline_value if separator else os.environ.get(key, "")
        environment.append({"name": key, "value": value})
    volume_mounts = [
        _mount_spec(source, target, readonly=readonly, shared_root=shared_root)
        for source, target, readonly in request.mounts
    ]
    container: dict[str, Any] = {
        "name": "main",
        "image": request.image,
        "imagePullPolicy": pull_policy,
        "env": environment,
        "volumeMounts": volume_mounts,
    }
    if request.command:
        container["args"] = request.command
    if request.memory:
        container["resources"] = {
            "requests": {"memory": _memory_quantity(request.memory)},
            "limits": {"memory": _memory_quantity(request.memory)},
        }
    if request.ports:
        container["ports"] = [{"containerPort": port} for port in sorted(set(request.ports))]
    volumes = (
        [{"name": "shared", "persistentVolumeClaim": {"claimName": pvc_name}}]
        if volume_mounts
        else []
    )
    pod: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "annotations": {_NAME_ANNOTATION: request.name},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Always" if request.detached else "Never",
            "securityContext": {
                "fsGroup": 3000,
                "fsGroupChangePolicy": "OnRootMismatch",
            },
            "containers": [container],
            "volumes": volumes,
        },
    }
    resources = [pod]
    if request.ports:
        resources.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "selector": selector,
                    "ports": [
                        {"name": f"tcp-{port}", "port": port, "targetPort": port}
                        for port in sorted(set(request.ports))
                    ],
                },
            }
        )
    return resources


def _write_kubeconfig() -> None:
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "in-cluster",
                "cluster": {
                    "server": "https://kubernetes.default.svc",
                    "certificate-authority": str(_SERVICE_ACCOUNT_ROOT / "ca.crt"),
                },
            }
        ],
        "users": [
            {
                "name": "service-account",
                "user": {"tokenFile": str(_SERVICE_ACCOUNT_ROOT / "token")},
            }
        ],
        "contexts": [
            {
                "name": "in-cluster",
                "context": {"cluster": "in-cluster", "user": "service-account"},
            }
        ],
        "current-context": "in-cluster",
    }
    _KUBECONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KUBECONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    _KUBECONFIG_PATH.chmod(0o600)


def kubectl_command() -> list[str]:
    _write_kubeconfig()
    return ["kubectl", "--kubeconfig", str(_KUBECONFIG_PATH)]


def _kubectl(*args: str, input_text: str | None = None, capture: bool = False) -> int:
    result = subprocess.run(  # noqa: S603 - kubectl prefix and resource commands are fixed.
        [*kubectl_command(), *args],
        input=input_text,
        text=True,
        capture_output=capture,
        check=False,
    )
    if capture:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
    return result.returncode


def _delete(runtime_name: str, grace_period: str) -> int:
    name = pod_name(runtime_name)
    service_result = _kubectl("delete", "service", name, "--ignore-not-found")
    pod_result = _kubectl(
        "delete",
        "pod",
        name,
        f"--grace-period={grace_period}",
        "--ignore-not-found",
    )
    return pod_result or service_result


def _run(args: list[str]) -> int:
    settings = runtime_settings()
    request = _parse_run(args)
    resources = build_resources(
        args,
        shared_root=settings.shared_root,
        pvc_name=settings.pvc_name,
        namespace=settings.namespace,
        pull_policy=settings.pull_policy,
    )
    payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": resources})
    result = _kubectl("apply", "-f", "-", input_text=payload, capture=True)
    if result or request.detached:
        return result
    name = pod_name(request.name)
    return _kubectl("logs", "-f", "--pod-running-timeout=120s", name)


def _inspect(args: list[str]) -> int:
    runtime_name = args[-1]
    name = pod_name(runtime_name)
    result = subprocess.run(  # noqa: S603 - fixed pod phase query.
        [*kubectl_command(), "get", "pod", name, "-o", "jsonpath={.status.phase}"],
        capture_output=True,
        check=False,
        text=True,
    )
    if "-f" in args:
        template = args[args.index("-f") + 1]
        if ".State.Running" in template:
            output = "true" if result.stdout == "Running" else "false"
        else:
            output = result.stdout.lower()
        sys.stdout.write(f"{output}\n")
    return result.returncode


def _logs(args: list[str]) -> int:
    name = pod_name(args[-1])
    kubectl_args = ["logs"]
    if "--tail" in args:
        tail_index = args.index("--tail")
        kubectl_args.append(f"--tail={args[tail_index + 1]}")
    kubectl_args.append(name)
    return _kubectl(*kubectl_args)


def run(args: list[str]) -> int:
    """Execute supported container CLI operation."""
    if not args:
        return 2
    command = args[0]
    if command == "run":
        result = _run(args)
    elif command == "inspect":
        result = _inspect(args)
    elif command == "logs":
        result = _logs(args)
    elif command == "stop":
        grace_period = args[args.index("-t") + 1] if "-t" in args else "5"
        result = _delete(args[-1], grace_period)
    elif command == "rm":
        result = _delete(args[-1], "0")
    elif command in {"image", "pull", "network"}:
        result = 0
    else:
        sys.stderr.write(f"Unsupported Kubernetes container operation: {command}\n")
        result = 2
    return result


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))
