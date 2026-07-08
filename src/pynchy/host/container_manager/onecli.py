"""OneCLI Agent Vault integration for container credential material.

OneCLI owns credential storage and request-time injection.  Pynchy only
materializes the container setup OneCLI returns: proxy env vars, CA files, and
credential stubs containing placeholders such as ``onecli-managed``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pynchy.config import get_settings
from pynchy.config.models import OneCliConfig
from pynchy.logger import logger
from pynchy.types import VolumeMount

_GATEWAY_SKILL_DIR = "onecli-gateway"
_GATEWAY_SKILL_MARKER = ".pynchy-onecli-skill"


class OneCliError(RuntimeError):
    """Base class for OneCLI integration failures."""


class OneCliAgentNotFound(OneCliError):
    """Raised when OneCLI has no agent for the requested identifier."""


@dataclass(frozen=True)
class OneCliMaterial:
    """Container setup material returned by OneCLI and written by Pynchy."""

    env_vars: dict[str, str]
    mounts: list[VolumeMount]
    warnings: list[str]


def normalize_agent_identifier(prefix: str, workspace: str) -> str:
    """Build a OneCLI agent identifier from a Pynchy workspace folder."""
    raw = f"{prefix}-{workspace}".lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        return "pynchy-agent"
    if not normalized[0].isalnum():
        normalized = f"pynchy-{normalized}"
    return normalized[:50].rstrip("-")


class OneCliClient:
    """Tiny sync client for the OneCLI API endpoints Pynchy needs at spawn time."""

    def __init__(
        self,
        *,
        config: OneCliConfig,
        api_key: str,
        project_id: str | None,
        timeout: float = 5,
    ) -> None:
        self._base_url = config.url.rstrip("/")
        self._api_key = api_key
        self._project_id = project_id
        self._timeout = timeout

    def get_container_config(self, *, agent: str) -> dict[str, Any]:
        query = urlencode({"agent": agent})
        try:
            return self._request_json("GET", f"/v1/container-config?{query}")
        except HTTPError as exc:
            if exc.code == 404:
                raise OneCliAgentNotFound(agent) from exc
            raise OneCliError(f"OneCLI container config failed with HTTP {exc.code}") from exc

    def create_agent(self, *, name: str, identifier: str) -> None:
        payload = {"name": name, "identifier": identifier}
        try:
            self._request_json("POST", "/v1/agents", payload=payload)
        except HTTPError as exc:
            if exc.code != 409:
                raise OneCliError(f"OneCLI agent create failed with HTTP {exc.code}") from exc

    def get_gateway_skill(self, *, agent_framework: str = "claude") -> str:
        query = urlencode({"agent_framework": agent_framework})
        return self._request_text("GET", f"/v1/skill/gateway?{query}")

    def health(self) -> dict[str, Any]:
        return self._request_json("GET", "/v1/health")

    def list_pending_approvals(self) -> dict[str, Any]:
        return self._request_json("GET", "/v1/approvals/pending")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self._project_id:
            headers["X-Project-Id"] = self._project_id

        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=self._timeout) as response:
            raw = response.read()
        try:
            data = json.loads(raw.decode() if raw else "{}")
        except json.JSONDecodeError as exc:
            raise OneCliError("OneCLI returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise OneCliError("OneCLI returned a non-object JSON response")
        return data

    def _request_text(self, method: str, path: str) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._project_id:
            headers["X-Project-Id"] = self._project_id

        request = Request(
            f"{self._base_url}{path}",
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=self._timeout) as response:
            raw: bytes = response.read()
            return raw.decode()


def prepare_onecli_material(group_folder: str) -> OneCliMaterial | None:
    """Fetch and write OneCLI material for a group, or return ``None`` when disabled."""
    settings = get_settings()
    config = settings.onecli
    if not config.enabled:
        return None

    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        _handle_unavailable(config, f"{config.api_key_env} is not set")
        return None

    project_id = os.environ.get(config.project_id_env)
    agent = normalize_agent_identifier(config.agent_identifier_prefix, group_folder)
    client = OneCliClient(config=config, api_key=api_key, project_id=project_id)

    try:
        try:
            container_config = client.get_container_config(agent=agent)
        except OneCliAgentNotFound:
            client.create_agent(name=group_folder, identifier=agent)
            container_config = client.get_container_config(agent=agent)
    except (HTTPError, URLError, OSError, TimeoutError, OneCliError) as exc:
        _handle_unavailable(config, str(exc))
        return None

    return _materialize_container_config(
        data_dir=settings.data_dir,
        group_folder=group_folder,
        container_config=container_config,
    )


def sync_onecli_gateway_skill(
    skills_dir: Path,
    *,
    agent_framework: str = "claude",
) -> None:
    """Install OneCLI's generated gateway skill into the session skill directory."""
    settings = get_settings()
    config = settings.onecli
    skill_dir = skills_dir / _GATEWAY_SKILL_DIR

    if not config.enabled:
        _remove_generated_gateway_skill(skill_dir)
        return

    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        _handle_unavailable(config, f"{config.api_key_env} is not set")
        return

    project_id = os.environ.get(config.project_id_env)
    client = OneCliClient(config=config, api_key=api_key, project_id=project_id)

    try:
        content = client.get_gateway_skill(agent_framework=agent_framework)
    except (HTTPError, URLError, OSError, TimeoutError, OneCliError) as exc:
        _handle_unavailable(config, str(exc))
        return

    if not content.strip():
        _handle_unavailable(config, "OneCLI gateway skill was empty")
        return

    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    (skill_dir / _GATEWAY_SKILL_MARKER).write_text("generated by pynchy\n")


def _remove_generated_gateway_skill(skill_dir: Path) -> None:
    marker = skill_dir / _GATEWAY_SKILL_MARKER
    if marker.exists():
        shutil.rmtree(skill_dir, ignore_errors=True)


def collect_onecli_status() -> dict[str, Any]:
    """Return a non-secret operational status snapshot for OneCLI."""
    settings = get_settings()
    config = settings.onecli
    result: dict[str, Any] = {
        "enabled": config.enabled,
        "url": config.url,
        "fail_closed": config.fail_closed,
    }
    if not config.enabled:
        return result

    api_key = os.environ.get(config.api_key_env)
    project_id = os.environ.get(config.project_id_env)
    result["api_key_configured"] = bool(api_key)
    result["project_id_configured"] = bool(project_id)
    if not api_key:
        result["ready"] = False
        result["error"] = f"{config.api_key_env} is not set"
        return result

    client = OneCliClient(config=config, api_key=api_key, project_id=project_id, timeout=0.75)
    try:
        health = client.health()
        result["ready"] = health.get("status") == "ok"
        if isinstance(health.get("version"), str):
            result["version"] = health["version"]
    except (HTTPError, URLError, OSError, TimeoutError, OneCliError) as exc:
        result["ready"] = False
        result["error"] = str(exc)
        return result

    try:
        approvals = client.list_pending_approvals()
        requests = approvals.get("requests", [])
        result["pending_approvals"] = len(requests) if isinstance(requests, list) else None
    except (HTTPError, URLError, OSError, TimeoutError, OneCliError) as exc:
        result["pending_approvals"] = None
        result["approvals_error"] = str(exc)

    return result


def _handle_unavailable(config: OneCliConfig, reason: str) -> None:
    if config.fail_closed:
        raise OneCliError(f"OneCLI is enabled but unavailable: {reason}")
    logger.warning("OneCLI unavailable; falling back to native credentials", reason=reason)
    return None


def _materialize_container_config(
    *,
    data_dir: Path,
    group_folder: str,
    container_config: dict[str, Any],
) -> OneCliMaterial:
    env_vars = _string_dict(container_config.get("env", {}), field="env")
    warnings = _string_list(container_config.get("warnings", []), field="warnings")
    base_dir = data_dir / "onecli" / group_folder
    mounts: list[VolumeMount] = []

    ca_certificate = container_config.get("caCertificate")
    ca_container_path = container_config.get("caCertificateContainerPath")
    if isinstance(ca_certificate, str) and isinstance(ca_container_path, str):
        ca_host_path = base_dir / "ca" / "onecli-ca.pem"
        ca_host_path.parent.mkdir(parents=True, exist_ok=True)
        ca_host_path.write_text(ca_certificate)
        ca_host_path.chmod(0o600)
        mounts.append(
            VolumeMount(
                str(ca_host_path),
                _normalize_container_path(ca_container_path),
                readonly=True,
            )
        )

    stubs = container_config.get("credentialStubs", [])
    if not isinstance(stubs, list):
        raise OneCliError("OneCLI credentialStubs must be a list")
    for stub in stubs:
        if not isinstance(stub, dict):
            raise OneCliError("OneCLI credential stub must be an object")
        container_path = stub.get("containerPath")
        content = stub.get("content")
        if not isinstance(container_path, str) or not isinstance(content, str):
            raise OneCliError("OneCLI credential stub needs containerPath and content")
        normalized_container_path = _normalize_container_path(container_path)
        host_path = base_dir / "stubs" / _safe_material_filename(normalized_container_path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(content)
        host_path.chmod(0o600)
        mounts.append(VolumeMount(str(host_path), normalized_container_path, readonly=True))

    return OneCliMaterial(env_vars=env_vars, mounts=mounts, warnings=warnings)


def _string_dict(value: Any, *, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise OneCliError(f"OneCLI {field} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise OneCliError(f"OneCLI {field} entries must be strings")
        result[key] = item
    return result


def _string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise OneCliError(f"OneCLI {field} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise OneCliError(f"OneCLI {field} entries must be strings")
    return list(value)


def _normalize_container_path(path: str) -> str:
    if path.startswith("~/"):
        return f"/home/agent/{path[2:]}"
    if path.startswith("/"):
        return path
    return f"/{path}"


def _safe_material_filename(container_path: str) -> str:
    digest = hashlib.sha256(container_path.encode()).hexdigest()[:12]
    name = Path(container_path).name or "stub"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return f"{digest}-{safe_name}"
