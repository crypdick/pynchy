"""Reusable fake Bitwarden CLI for Vaultwarden administration tests."""

from __future__ import annotations

import base64
import json
import subprocess  # noqa: S404 - fake CompletedProcess values only.
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from pynchy.plugins.integrations.vaultwarden import VaultwardenAdminRuntime

FINANCE_ID = "11111111-1111-4111-8111-111111111111"
SHARED_ID = "22222222-2222-4222-8222-222222222222"
NEW_ID = "33333333-3333-4333-8333-333333333333"


def collection_grant(identifier: str) -> dict[str, object]:
    return {"id": identifier, "readOnly": False, "hidePasswords": False, "manage": False}


class FakeAdminBw:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.environments: list[dict[str, str]] = []
        self.failure_prefix: tuple[str, ...] | None = None
        self.failure_stderr = ""
        self.raw_outputs: dict[tuple[str, ...], str] = {}
        self.visible = {
            "finance": [FINANCE_ID, SHARED_ID],
            "systems": [SHARED_ID],
        }
        self.items: dict[str, dict[str, Any]] = {
            "source-id": {
                "id": "source-id",
                "name": "Source",
                "login": {
                    "username": "person@example.test",
                    "password": "source-secret",  # pragma: allowlist secret
                    "uris": [{"uri": "https://example.test"}],
                    "totp": "excluded-totp",  # pragma: allowlist secret
                },
            }
        }
        self.members = [
            {"id": "finance-member", "email": "finance@example.test"},
            {"id": "systems-member", "email": "systems@example.test"},
            {"id": "external-member", "email": "external@example.test"},
        ]
        self.org_collections: dict[str, dict[str, Any]] = {
            FINANCE_ID: {
                "id": FINANCE_ID,
                "name": "finance",
                "organizationId": "organization-id",
                "groups": [],
                "users": [collection_grant("finance-member")],
            },
            SHARED_ID: {
                "id": SHARED_ID,
                "name": "shared",
                "organizationId": "organization-id",
                "groups": [],
                "users": [
                    collection_grant("finance-member"),
                    collection_grant("systems-member"),
                    collection_grant("external-member"),
                ],
            },
        }

    def __call__(
        self,
        args: list[str],
        *,
        env: dict[str, str],
        input_value: str | None = None,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        subprocess_input = kwargs.get("input")
        if input_value is None and isinstance(subprocess_input, str):
            input_value = subprocess_input
        command = tuple(args[1:])
        self.calls.append((command, input_value))
        self.environments.append(env)
        if (
            self.failure_prefix is not None
            and command[: len(self.failure_prefix)] == self.failure_prefix
        ):
            return subprocess.CompletedProcess(args, 1, "", self.failure_stderr)
        for prefix, raw_output in self.raw_outputs.items():
            if command[: len(prefix)] == prefix:
                return subprocess.CompletedProcess(args, 0, raw_output, "")
        account = Path(env["BITWARDENCLI_APPDATA_DIR"]).name
        output = self._output(command, input_value, account)
        return subprocess.CompletedProcess(args, 0, output, "")

    def _output(self, command: tuple[str, ...], input_value: str | None, account: str) -> str:
        if command[:2] == ("config", "server"):
            output = "https://vault.pynchy.svc.cluster.local\n"
        elif command == ("status",):
            output = json.dumps({"status": "locked"})
        elif command[:1] == ("unlock",):
            output = f"session-{account}\n"
        elif command[:2] == ("list", "collections"):
            output = json.dumps([{"id": item} for item in self.visible[account]])
        elif command[:2] == ("list", "organizations"):
            output = json.dumps([{"id": "organization-id"}])
        elif command[:2] == ("list", "org-members"):
            output = json.dumps(self.members)
        elif command[:2] == ("list", "items"):
            search = command[command.index("--search") + 1]
            output = json.dumps([item for item in self.items.values() if item["name"] == search])
        elif command[:2] == ("edit", "item-collections"):
            self.items[command[2]]["collectionIds"] = _decoded(input_value)
            output = json.dumps(self.items[command[2]])
        elif command[:2] in {("create", "item"), ("edit", "item")}:
            item = _decoded(input_value)
            identifier = "created-id" if command[0] == "create" else command[2]
            item["id"] = identifier
            self.items[identifier] = item
            output = json.dumps(item)
        elif command[:2] == ("get", "org-collection"):
            output = json.dumps(self.org_collections[command[2]])
        elif command[:2] == ("create", "org-collection"):
            collection = _decoded(input_value)
            collection["id"] = NEW_ID
            self.org_collections[NEW_ID] = collection
            output = json.dumps(collection)
        elif command[:2] == ("edit", "org-collection"):
            collection = _decoded(input_value)
            collection["id"] = command[2]
            self.org_collections[command[2]] = collection
            output = json.dumps(collection)
        elif command[:2] == ("delete", "org-collection"):
            self.org_collections.pop(command[2], None)
            output = ""
        else:
            output = ""
        return output


def admin_runtime(
    tmp_path: Path,
    *,
    update_channel_collections=lambda _channel, _collections: None,
    add_collection=lambda _alias, _identifier, _channels: None,
) -> VaultwardenAdminRuntime:
    return VaultwardenAdminRuntime(
        server_url="https://vault.pynchy.svc.cluster.local",
        collections={"finance": FINANCE_ID, "shared": SHARED_ID},
        data_dir=tmp_path,
        channel_collections={"finance": ("finance", "shared"), "systems": ("shared",)},
        update_channel_collections=update_channel_collections,
        add_collection=add_collection,
    )


def set_admin_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for account in ("ADMIN", "FINANCE", "SYSTEMS"):
        monkeypatch.setenv(f"PYNCHY_VAULTWARDEN_{account}_EMAIL", f"{account.lower()}@example.test")
        monkeypatch.setenv(f"PYNCHY_VAULTWARDEN_{account}_PASSWORD", "master-password")


def _decoded(value: str | None) -> Any:
    assert value is not None
    return json.loads(base64.b64decode(value))
