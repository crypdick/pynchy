"""Channel-scoped Vaultwarden reads through the host-only Bitwarden CLI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess  # noqa: S404 - broker invokes one fixed host binary without a shell.
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - Pydantic/beartype resolve runtime annotations.
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID  # noqa: TC003 - Pydantic resolves runtime annotations.

import pluggy
from pydantic import BaseModel, ConfigDict, field_validator

from pynchy.actions.api import (
    ActionId,
)
from pynchy.atomic_json import write_json_atomic
from pynchy.host.paths import PYNCHY_SECRETS_CONTAINER_PATH
from pynchy.plugins.api import (
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityRequirement,
    CapabilityRequirementKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.plugins.integrations._service import service_tool
from pynchy.process_environment import filtered_process_environment

hookimpl = pluggy.HookimplMarker("pynchy")

_PLUGIN_NAME = "vaultwarden"
_TOOL_NAME = "get_secret"
_ACTION_ID = "secret.vaultwarden.read"
_ENV_PREFIX = "PYNCHY_VAULTWARDEN_"
_CA_CERT_PATH = "/etc/pynchy-vaultwarden/ca.crt"


class VaultwardenOptions(BaseModel):
    """Host-only Vaultwarden endpoint and collection identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_url: str
    collections: dict[str, UUID]

    @field_validator("server_url")
    @classmethod
    def require_cluster_https_server(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.hostname is None
            or not parsed.hostname.endswith(".svc.cluster.local")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Vaultwarden server_url must be a cluster-local HTTPS origin")
        return value.rstrip("/")


@dataclass(frozen=True)
class VaultwardenRuntime:
    """Composition-owned inputs for one host broker."""

    options: VaultwardenOptions
    data_dir: Path
    resolve_access: Callable[[str], tuple[str, tuple[str, ...]] | None]


type BwRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run_bw(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and argv; no shell.
        args,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class VaultwardenBroker:
    """Fetch exact-name items without exposing CLI credentials or raw vault records."""

    def __init__(self, runtime: VaultwardenRuntime, *, run: BwRunner = _run_bw) -> None:
        self.runtime = runtime
        self._run = run
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def get_secret(self, source_group: str, name: str) -> dict[str, object]:
        if not name or len(name) > 256:
            raise ValueError("secret name must contain 1 to 256 characters")
        access = self.runtime.resolve_access(source_group)
        if access is None:
            raise PermissionError("secret access is not enabled for this workspace")
        account, aliases = access
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", account) is None:
            raise ValueError("Vaultwarden account name is invalid")
        with self._locks[account]:
            return self._get_secret_locked(source_group, name, account, aliases)

    def _get_secret_locked(
        self, source_group: str, name: str, account: str, aliases: tuple[str, ...]
    ) -> dict[str, object]:
        collection_ids = [str(self.runtime.options.collections[alias]) for alias in aliases]
        environment, secrets_to_redact = self._environment(account)
        appdata = self.runtime.data_dir / "vaultwarden-cli" / account
        appdata.mkdir(parents=True, exist_ok=True, mode=0o700)
        appdata.chmod(0o700)
        environment["BITWARDENCLI_APPDATA_DIR"] = str(appdata)

        if not (appdata / "data.json").exists():
            self._checked(
                ["bw", "config", "server", self.runtime.options.server_url],
                environment,
                secrets_to_redact,
            )
        configured_server = (
            self._checked(["bw", "config", "server"], environment, secrets_to_redact)
            .strip()
            .rstrip("/")
        )
        if configured_server != self.runtime.options.server_url:
            raise ValueError("Bitwarden CLI server does not match configured Vaultwarden server")

        status = self._json_command(["bw", "status"], environment, secrets_to_redact)
        if not isinstance(status, dict):
            raise TypeError("Bitwarden CLI returned an invalid status")
        if status.get("status") == "unauthenticated":
            self._checked(
                [
                    "bw",
                    "login",
                    environment["BW_USERNAME"],
                    "--passwordenv",
                    "BW_PASSWORD",
                ],
                environment,
                secrets_to_redact,
            )
        session = self._checked(
            ["bw", "unlock", "--passwordenv", "BW_PASSWORD", "--raw"],
            environment,
            secrets_to_redact,
        ).strip()
        if not session:
            raise ValueError("Bitwarden CLI returned an empty session")
        try:
            self._checked(
                ["bw", "sync", "--session", session],
                environment,
                [*secrets_to_redact, session],
            )
            matches = self._find_exact_items(
                name,
                collection_ids,
                session,
                environment,
                secrets_to_redact,
            )
            if len(matches) != 1:
                raise ValueError(f"expected exactly one item named {name!r}; found {len(matches)}")
            normalized = _normalized_secret(next(iter(matches.values())))
            if not normalized:
                raise ValueError(f"item {name!r} contains no supported login fields")
            secret_dir = self.runtime.data_dir / "ipc" / source_group / "secrets"
            filename = f"{os.urandom(16).hex()}.json"
            write_json_atomic(secret_dir / filename, normalized, mode=0o600)
            return {
                "path": f"{PYNCHY_SECRETS_CONTAINER_PATH}/{filename}",
                "keys": sorted(normalized),
            }
        finally:
            self._run(["bw", "lock"], env=environment)

    def _find_exact_items(
        self,
        name: str,
        collection_ids: list[str],
        session: str,
        environment: dict[str, str],
        redactions: list[str],
    ) -> dict[str, dict[str, Any]]:
        matches: dict[str, dict[str, Any]] = {}
        for collection_id in collection_ids:
            items = self._json_command(
                [
                    "bw",
                    "list",
                    "items",
                    "--collectionid",
                    collection_id,
                    "--search",
                    name,
                    "--session",
                    session,
                ],
                environment,
                [*redactions, session],
            )
            if not isinstance(items, list):
                raise TypeError("Bitwarden CLI returned an invalid item list")
            for item in items:
                if isinstance(item, dict) and item.get("name") == name:
                    item_id = item.get("id")
                    if isinstance(item_id, str):
                        matches[item_id] = item
        return matches

    def _environment(self, account: str) -> tuple[dict[str, str], list[str]]:
        suffix = re.sub(r"[^A-Za-z0-9]", "_", account).upper()
        names = {
            "BW_USERNAME": f"{_ENV_PREFIX}{suffix}_EMAIL",
            "BW_PASSWORD": f"{_ENV_PREFIX}{suffix}_PASSWORD",
        }
        missing = [source for source in names.values() if not os.environ.get(source)]
        if missing:
            raise ValueError(f"Vaultwarden account credentials are unavailable for {account!r}")
        values = {target: os.environ[source] for target, source in names.items()}
        values["NODE_EXTRA_CA_CERTS"] = _CA_CERT_PATH
        return filtered_process_environment(values), list(values.values())

    def _checked(self, args: list[str], environment: dict[str, str], redactions: list[str]) -> str:
        result = self._run(args, env=environment)
        if result.returncode == 0:
            return result.stdout
        error = result.stderr.strip() or "Bitwarden CLI command failed"
        for value in redactions:
            error = error.replace(value, "[REDACTED]")
        raise ValueError(error[:1000])

    def _json_command(
        self, args: list[str], environment: dict[str, str], redactions: list[str]
    ) -> object:
        raw = self._checked(args, environment, redactions)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Bitwarden CLI returned invalid JSON") from exc


def _normalized_secret(item: dict[str, Any]) -> dict[str, str]:
    login = item.get("login")
    if not isinstance(login, dict):
        return {}
    result: dict[str, str] = {}
    username = login.get("username")
    password = login.get("password")
    if isinstance(username, str) and username:
        result["login"] = username
        if "@" in username:
            result["email"] = username
    if isinstance(password, str) and password:
        result["password"] = password
    return result


@dataclass
class _RuntimeState:
    broker: VaultwardenBroker | None = None


_runtime = _RuntimeState()


def configure_vaultwarden_runtime(runtime: VaultwardenRuntime) -> None:
    _runtime.broker = VaultwardenBroker(runtime)


@service_tool
async def _handle_get_secret(data: dict[str, Any]) -> dict[str, Any]:
    if _runtime.broker is None:
        raise RuntimeError("Vaultwarden runtime has not been configured")
    source_group = data.get("source_group")
    name = data.get("name")
    if not isinstance(source_group, str) or not isinstance(name, str):
        raise TypeError("get_secret requires a secret name")
    result = await asyncio.to_thread(_runtime.broker.get_secret, source_group, name)
    return {"result": result}


VAULTWARDEN_HOST_ACTIONS = HostActionRegistration(
    actions=(
        HostActionDescriptor(
            capability=CapabilityDescriptor(
                id=CapabilityId(_ACTION_ID),
                kind=CapabilityKind.HOST_ACTION,
                owner=_PLUGIN_NAME,
                summary="Read one exact-name secret from channel-granted Vaultwarden collections.",
                action_ids=(ActionId(_ACTION_ID),),
                requirements=(
                    CapabilityRequirement(
                        kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                        name=_PLUGIN_NAME,
                        description="Grant Vaultwarden collections to the source Discord channel.",
                    ),
                    CapabilityRequirement(
                        kind=CapabilityRequirementKind.HOST_BINARY,
                        name="bw",
                        description="Install the pinned Bitwarden CLI in the host runtime.",
                    ),
                ),
                documentation="docs/usage/secrets.md",
            ),
            tool_name=HostToolName(_TOOL_NAME),
            handler=_handle_get_secret,
            access=HostActionAccess.READ,
            approval=ApprovalContract(),
            idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
            audit=AuditContract(),
            policy_service=_PLUGIN_NAME,
        ),
    )
)


class VaultwardenPlugin:
    """Expose channel-scoped secret reads through host IPC."""

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return VAULTWARDEN_HOST_ACTIONS
