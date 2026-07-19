"""Narrow host-only wrapper around the Gog executable.

The wrapper owns every permitted Gog command. Agent-provided values become
typed arguments or standard input; agents never provide command paths, Gog
flags, OAuth paths, or account selectors.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # noqa: S404 - this module constructs allowlisted Gog argv without a shell.
from dataclasses import dataclass
from pathlib import Path

from pynchy.config import get_settings
from pynchy.plugins.integrations.gog._config import GogConfig, gog_config

_MAX_OUTPUT_CHARS = 2_000_000
_OAUTH_SERVICES = "gmail,contacts,docs,sheets,drive"


class GogError(RuntimeError):
    """A safe, user-actionable Gog integration failure."""


@dataclass(frozen=True)
class GogClient:
    """Execute only Pynchy's reviewed Gog command surface on the host."""

    config: GogConfig
    home: Path
    oauth_client_path: Path | None

    def gmail_search(self, *, query: str, limit: int) -> str:
        return self._json(
            "gmail.search",
            ["gmail", "search", "--max", str(limit), "--", query],
            readonly=True,
        )

    def gmail_get(self, *, message_id: str) -> str:
        return self._json(
            "gmail.get",
            ["gmail", "get", "--sanitize-content", "--", message_id],
            readonly=True,
        )

    def gmail_create_draft(
        self,
        *,
        to: list[str],
        cc: list[str],
        bcc: list[str],
        subject: str,
        body: str,
    ) -> str:
        args = [
            "gmail",
            "drafts",
            "create",
            "--to",
            ",".join(to),
            "--subject",
            subject,
            "--body-file",
            "-",
        ]
        _append_recipients(args, "--cc", cc)
        _append_recipients(args, "--bcc", bcc)
        return self._json("gmail.drafts.create", args, stdin=body)

    def gmail_send_draft(self, *, draft_id: str) -> str:
        return self._json("gmail.drafts.send", ["gmail", "drafts", "send", "--", draft_id])

    def gmail_send(
        self,
        *,
        to: list[str],
        cc: list[str],
        bcc: list[str],
        subject: str,
        body: str,
    ) -> str:
        args = [
            "gmail",
            "send",
            "--to",
            ",".join(to),
            "--subject",
            subject,
            "--body-file",
            "-",
        ]
        _append_recipients(args, "--cc", cc)
        _append_recipients(args, "--bcc", bcc)
        return self._json("gmail.send", args, stdin=body)

    def contacts_search(self, *, query: str, limit: int) -> str:
        return self._json(
            "contacts.search",
            ["contacts", "search", "--max", str(limit), "--", query],
            readonly=True,
        )

    def docs_read(self, *, document_id: str, tab: str | None) -> str:
        args = ["docs", "cat", "--max-bytes", str(_MAX_OUTPUT_CHARS)]
        if tab is not None:
            args.extend(["--tab", tab])
        args.extend(["--", document_id])
        return self._text("docs.cat", args, readonly=True)

    def docs_export(self, *, document_id: str, export_format: str) -> str:
        return self._text(
            "docs.export",
            ["docs", "export", "--format", export_format, "--out", "-", "--", document_id],
            readonly=True,
        )

    def sheets_get(self, *, spreadsheet_id: str, range_name: str) -> str:
        return self._json(
            "sheets.get",
            ["sheets", "get", "--", spreadsheet_id, range_name],
            readonly=True,
        )

    def sheets_update(
        self,
        *,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[str | int | float | bool | None]],
        input_mode: str,
    ) -> str:
        values_json = json.dumps(values, allow_nan=False, separators=(",", ":"))
        return self._json(
            "sheets.update",
            [
                "sheets",
                "update",
                "--values-json",
                "@-",
                "--input",
                input_mode,
                "--fail-on-formula-error",
                "--",
                spreadsheet_id,
                range_name,
            ],
            stdin=values_json,
        )

    def setup_start(self) -> str:
        """Store the configured OAuth client and start Gog's split remote flow."""
        oauth_client_path = self._oauth_client_path()
        self._json("auth.credentials.set", ["auth", "credentials", "set", str(oauth_client_path)])
        return self._json(
            "auth.add",
            [
                "auth",
                "add",
                self._account(),
                "--services",
                _OAUTH_SERVICES,
                "--drive-scope",
                "readonly",
                "--remote",
                "--step",
                "1",
            ],
        )

    def setup_complete(self, *, redirect_url: str) -> str:
        """Exchange a user-returned OAuth redirect URL without exposing tokens."""
        return self._json(
            "auth.add",
            [
                "auth",
                "add",
                self._account(),
                "--services",
                _OAUTH_SERVICES,
                "--drive-scope",
                "readonly",
                "--remote",
                "--step",
                "2",
                "--auth-url",
                redirect_url,
            ],
        )

    def _json(
        self,
        allowed_command: str,
        arguments: list[str],
        *,
        readonly: bool = False,
        stdin: str | None = None,
    ) -> str:
        output = self._run(
            allowed_command,
            arguments,
            as_json=True,
            readonly=readonly,
            stdin=stdin,
        )
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GogError("Gog returned invalid JSON") from exc
        if not isinstance(parsed, dict | list):
            raise GogError("Gog returned an unsupported JSON result")
        return json.dumps(parsed, sort_keys=True)

    def _text(self, allowed_command: str, arguments: list[str], *, readonly: bool) -> str:
        return self._run(allowed_command, arguments, as_json=False, readonly=readonly)

    def _run(
        self,
        allowed_command: str,
        arguments: list[str],
        *,
        as_json: bool,
        readonly: bool,
        stdin: str | None = None,
    ) -> str:
        command = [
            self.config.command,
            "--home",
            str(self.home),
            "--account",
            self._account(),
            "--no-input",
            "--enable-commands-exact",
            allowed_command,
        ]
        if as_json:
            command.append("--json")
        if readonly:
            command.extend(["--readonly", "--wrap-untrusted", "--gmail-no-send"])
        command.extend(arguments)
        try:
            result = subprocess.run(  # noqa: S603 - argv comes only from typed, allowlisted builders.
                command,
                check=False,
                capture_output=True,
                input=stdin,
                text=True,
                timeout=self.config.timeout_seconds,
                env=_gog_environment(self.home),
            )
        except FileNotFoundError as exc:
            raise GogError(
                "Gog is unavailable; install gogcli or configure plugins.gog.options.command"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GogError("Gog command timed out") from exc
        if result.returncode:
            raise GogError("Gog command failed; run `pynchy doctor` for safe readiness details")
        if len(result.stdout) > _MAX_OUTPUT_CHARS:
            raise GogError("Gog response exceeded Pynchy's safe output limit")
        if not result.stdout.strip():
            raise GogError("Gog command returned no data")
        return result.stdout

    def _account(self) -> str:
        if self.config.account is None:
            raise GogError("Configure plugins.gog.options.account before using Gog")
        return self.config.account

    def _oauth_client_path(self) -> Path:
        if self.oauth_client_path is None:
            raise GogError(
                "Configure plugins.gog.options.oauth_client_path before starting Gog OAuth"
            )
        if not self.oauth_client_path.is_file():
            raise GogError("Configured Gog OAuth client credentials are unavailable on the host")
        return self.oauth_client_path


def create_gog_client() -> GogClient:
    """Build the production client from plugin configuration and host settings."""
    settings = get_settings()
    config = gog_config()
    return GogClient(
        config=config,
        home=config.resolved_home(settings),
        oauth_client_path=config.resolved_oauth_client_path(settings),
    )


def gog_executable_exists(command: str) -> bool:
    """Check local executable presence without invoking Gog or Google."""
    path = Path(command).expanduser()
    if path.parent != Path():
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


def _append_recipients(arguments: list[str], flag: str, recipients: list[str]) -> None:
    if recipients:
        arguments.extend([flag, ",".join(recipients)])


def _gog_environment(home: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["GOG_HOME"] = str(home)
    return environment
