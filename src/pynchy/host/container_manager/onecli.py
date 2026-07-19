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
from typing import Any, Protocol, cast, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from pynchy.config import get_settings
from pynchy.config.models import (
    OneCliConfig,  # noqa: TC001, RUF100 - beartype resolves OneCLI client signatures at runtime.
)
from pynchy.host.container_manager.gateway import resolve_container_host
from pynchy.logger import logger
from pynchy.types import VolumeMount

_GATEWAY_SKILL_DIR = "onecli-gateway"
_GATEWAY_SKILL_MARKER = ".pynchy-onecli-skill"
_HTTP_CONTAINER_CONFIG_ERROR = "OneCLI container config failed with HTTP {}"
_HTTP_AGENT_CREATE_ERROR = "OneCLI agent create failed with HTTP {}"
_INVALID_JSON_ERROR = "OneCLI returned invalid JSON"
_NON_OBJECT_JSON_ERROR = "OneCLI returned a non-object JSON response"
_URL_SCHEME_ERROR = "OneCLI URL must use http or https"
_UNAVAILABLE_ERROR = "OneCLI is enabled but unavailable: {}"
_CREDENTIAL_STUBS_LIST_ERROR = "OneCLI credentialStubs must be a list"
_CREDENTIAL_STUB_OBJECT_ERROR = "OneCLI credential stub must be an object"
_CREDENTIAL_STUB_FIELDS_ERROR = "OneCLI credential stub needs containerPath and content"
_OBJECT_FIELD_ERROR = "OneCLI {} must be an object"
_STRING_ENTRIES_ERROR = "OneCLI {} entries must be strings"
_LIST_FIELD_ERROR = "OneCLI {} must be a list"
_CONTAINER_PROXY_ENV_KEYS = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
)
_DEFAULT_CONTAINER_PROXY_HOST = "host.docker.internal"


@runtime_checkable
class _UrlopenResponse(Protocol):
    def __enter__(self) -> _UrlopenResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: object,
    ) -> object: ...

    def read(self) -> bytes: ...


class OneCliError(RuntimeError):
    """Base class for OneCLI integration failures."""


class OneCliAgentNotFoundError(OneCliError):
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
                raise OneCliAgentNotFoundError(agent) from exc
            raise OneCliError(_HTTP_CONTAINER_CONFIG_ERROR.format(exc.code)) from exc

    def create_agent(self, *, name: str, identifier: str) -> None:
        payload = {"name": name, "identifier": identifier}
        try:
            self._request_json("POST", "/v1/agents", payload=payload)
        except HTTPError as exc:
            if exc.code != 409:
                raise OneCliError(_HTTP_AGENT_CREATE_ERROR.format(exc.code)) from exc

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

        request = Request(  # noqa: S310, RUF100 - opened only through the HTTP(S)-gated helper.
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        with _urlopen_http_request(request, timeout=self._timeout) as response:
            raw = response.read()
        try:
            data = json.loads(raw.decode() if raw else "{}")
        except json.JSONDecodeError as exc:
            raise OneCliError(_INVALID_JSON_ERROR) from exc
        if not isinstance(data, dict):
            raise OneCliError(_NON_OBJECT_JSON_ERROR)
        return data

    def _request_text(self, method: str, path: str) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._project_id:
            headers["X-Project-Id"] = self._project_id

        request = Request(  # noqa: S310, RUF100 - opened only through the HTTP(S)-gated helper.
            f"{self._base_url}{path}",
            headers=headers,
            method=method,
        )
        with _urlopen_http_request(request, timeout=self._timeout) as response:
            raw: bytes = response.read()
            return raw.decode()


def _urlopen_http_request(request: Request, *, timeout: int | float) -> _UrlopenResponse:
    scheme = urlsplit(request.full_url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise OneCliError(_URL_SCHEME_ERROR)
    return cast(
        "_UrlopenResponse",
        urlopen(request, timeout=timeout),  # noqa: S310, RUF100 - scheme is constrained above.
    )


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
        except OneCliAgentNotFoundError:
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
        result["egress_pending_approvals"] = len(requests) if isinstance(requests, list) else None
    except (HTTPError, URLError, OSError, TimeoutError, OneCliError) as exc:
        result["egress_pending_approvals"] = None
        result["egress_approvals_error"] = str(exc)

    return result


def _handle_unavailable(config: OneCliConfig, reason: str) -> None:
    if config.fail_closed:
        raise OneCliError(_UNAVAILABLE_ERROR.format(reason))
    logger.warning("OneCLI unavailable; falling back to native credentials", reason=reason)


def _materialize_container_config(
    *,
    data_dir: Path,
    group_folder: str,
    container_config: dict[str, Any],
) -> OneCliMaterial:
    env_vars = _resolve_container_proxy_hosts(
        _string_dict(container_config.get("env", {}), field="env")
    )
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
        raise OneCliError(_CREDENTIAL_STUBS_LIST_ERROR)
    for stub in stubs:
        if not isinstance(stub, dict):
            raise OneCliError(_CREDENTIAL_STUB_OBJECT_ERROR)
        container_path = stub.get("containerPath")
        content = stub.get("content")
        if not isinstance(container_path, str) or not isinstance(content, str):
            raise OneCliError(_CREDENTIAL_STUB_FIELDS_ERROR)
        normalized_container_path = _normalize_container_path(container_path)
        host_path = base_dir / "stubs" / _safe_material_filename(normalized_container_path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(content)
        host_path.chmod(0o600)
        mounts.append(VolumeMount(str(host_path), normalized_container_path, readonly=True))

    return OneCliMaterial(env_vars=env_vars, mounts=mounts, warnings=warnings)


def _resolve_container_proxy_hosts(env_vars: dict[str, str]) -> dict[str, str]:
    """Adapt OneCLI's Docker-default proxy hostname to the active runtime."""
    resolved_host = resolve_container_host(_DEFAULT_CONTAINER_PROXY_HOST)
    if resolved_host == _DEFAULT_CONTAINER_PROXY_HOST:
        return env_vars
    return {
        key: (
            value.replace(_DEFAULT_CONTAINER_PROXY_HOST, resolved_host)
            if key in _CONTAINER_PROXY_ENV_KEYS
            else value
        )
        for key, value in env_vars.items()
    }


def _string_dict(value: object, *, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise OneCliError(_OBJECT_FIELD_ERROR.format(field))
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise OneCliError(_STRING_ENTRIES_ERROR.format(field))
        result[key] = item
    return result


def _string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise OneCliError(_LIST_FIELD_ERROR.format(field))
    if not all(isinstance(item, str) for item in value):
        raise OneCliError(_STRING_ENTRIES_ERROR.format(field))
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
