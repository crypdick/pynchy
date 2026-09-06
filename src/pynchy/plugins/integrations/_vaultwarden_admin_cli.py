"""Host-only Bitwarden CLI session for Vaultwarden administration."""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404 - client invokes one fixed host binary without a shell.
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pynchy.process_environment import filtered_process_environment

_ENV_PREFIX = "PYNCHY_VAULTWARDEN_"
_CA_CERT_PATH = "/etc/pynchy-vaultwarden/ca.crt"

type AdminBwRunner = Callable[..., subprocess.CompletedProcess[str]]


def run_bw(
    args: list[str], *, env: dict[str, str], input_value: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and argv; no shell.
        args,
        env=env,
        input=input_value,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@dataclass(frozen=True)
class BwSession:
    """Unlocked CLI session whose errors redact credentials and payloads."""

    token: str
    environment: dict[str, str]
    redactions: tuple[str, ...]
    run: AdminBwRunner

    def checked(
        self,
        args: list[str],
        *,
        input_value: str | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> str:
        redactions = (
            *self.redactions,
            self.token,
            *((input_value,) if input_value else ()),
            *sensitive_values,
        )
        result = self.run(args, env=self.environment, input_value=input_value)
        if result.returncode == 0:
            return result.stdout
        error = result.stderr.strip() or "Bitwarden CLI command failed"
        for value in redactions:
            error = error.replace(value, "[REDACTED]")
        raise ValueError(error[:1000])

    def json(
        self,
        args: list[str],
        *,
        input_value: str | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> object:
        raw = self.checked(args, input_value=input_value, sensitive_values=sensitive_values)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Bitwarden CLI returned invalid JSON") from exc


class BwClient:
    """Configure, authenticate, synchronize, and lock one CLI profile."""

    def __init__(self, server_url: str, data_dir: Path, *, run: AdminBwRunner = run_bw) -> None:
        self.server_url = server_url
        self.data_dir = data_dir
        self.run = run

    @contextmanager
    def session(self, account: str) -> Iterator[BwSession]:
        environment, credentials = self._environment(account)
        appdata = self.data_dir / "vaultwarden-cli" / account
        appdata.mkdir(parents=True, exist_ok=True, mode=0o700)
        appdata.chmod(0o700)
        environment["BITWARDENCLI_APPDATA_DIR"] = str(appdata)
        if not (appdata / "data.json").exists():
            self._checked(["bw", "config", "server", self.server_url], environment, credentials)
        configured = self._checked(["bw", "config", "server"], environment, credentials).strip()
        if configured.rstrip("/") != self.server_url:
            raise ValueError("Bitwarden CLI server does not match configured Vaultwarden server")
        status = self._json(["bw", "status"], environment, credentials)
        if not isinstance(status, dict):
            raise TypeError("Bitwarden CLI returned an invalid status")
        if status.get("status") == "unauthenticated":
            self._checked(
                ["bw", "login", environment["BW_USERNAME"], "--passwordenv", "BW_PASSWORD"],
                environment,
                credentials,
            )
        token = self._checked(
            ["bw", "unlock", "--passwordenv", "BW_PASSWORD", "--raw"],
            environment,
            credentials,
        ).strip()
        if not token:
            raise ValueError("Bitwarden CLI returned an empty session")
        session = BwSession(token, environment, (*credentials, token), self.run)
        try:
            session.checked(["bw", "sync", "--session", token])
            yield session
        finally:
            self.run(["bw", "lock"], env=environment, input_value=None)

    def account_email(self, account: str) -> str:
        suffix = re.sub(r"[^A-Za-z0-9]", "_", account).upper()
        value = os.environ.get(f"{_ENV_PREFIX}{suffix}_EMAIL")
        if not value:
            raise ValueError(f"Vaultwarden account credentials are unavailable for {account!r}")
        return value

    def _environment(self, account: str) -> tuple[dict[str, str], tuple[str, ...]]:
        suffix = re.sub(r"[^A-Za-z0-9]", "_", account).upper()
        sources = {
            "BW_USERNAME": f"{_ENV_PREFIX}{suffix}_EMAIL",
            "BW_PASSWORD": f"{_ENV_PREFIX}{suffix}_PASSWORD",
        }
        if any(not os.environ.get(source) for source in sources.values()):
            raise ValueError(f"Vaultwarden account credentials are unavailable for {account!r}")
        values = {target: os.environ[source] for target, source in sources.items()}
        values["NODE_EXTRA_CA_CERTS"] = _CA_CERT_PATH
        return filtered_process_environment(values), tuple(values.values())

    def _checked(
        self, args: list[str], environment: dict[str, str], redactions: tuple[str, ...]
    ) -> str:
        result = self.run(args, env=environment, input_value=None)
        if result.returncode == 0:
            return result.stdout
        error = result.stderr.strip() or "Bitwarden CLI command failed"
        for value in redactions:
            error = error.replace(value, "[REDACTED]")
        raise ValueError(error[:1000])

    def _json(
        self, args: list[str], environment: dict[str, str], redactions: tuple[str, ...]
    ) -> object:
        raw = self._checked(args, environment, redactions)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Bitwarden CLI returned invalid JSON") from exc
