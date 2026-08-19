"""Vaultwarden administration keeps provider secrets behind a typed host action."""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - fake CompletedProcess values only.
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

import pytest
import tomlkit
from vaultwarden_admin_test_support import (
    FINANCE_ID,
    NEW_ID,
    SHARED_ID,
)
from vaultwarden_admin_test_support import (
    FakeAdminBw as _FakeAdminBw,
)
from vaultwarden_admin_test_support import (
    admin_runtime as _runtime,
)
from vaultwarden_admin_test_support import (
    collection_grant as _grant,
)
from vaultwarden_admin_test_support import (
    set_admin_credentials as _credentials,
)

from pynchy.config.api import Settings
from pynchy.host.orchestrator.plugin_configuration import configure_vaultwarden_plugin
from pynchy.plugins.api import ApprovalTrigger, HostActionAccess, IdempotencyMode
from pynchy.plugins.integrations.vaultwarden import (
    VAULTWARDEN_HOST_ACTIONS,
    VaultwardenAdminBroker,
    VaultwardenOptions,
    VaultwardenRuntime,
    configure_vaultwarden_runtime,
)


def test_verify_access_checks_each_channel_account_without_returning_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=fake)

    result = broker.execute({"operation": "verify_access"})

    assert result == {
        "channels": {"finance": ["finance", "shared"], "systems": ["shared"]},
        "verified": True,
    }
    assert "master-password" not in json.dumps(result)


def test_verify_access_fails_closed_on_collection_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    fake.visible["systems"] = []
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=fake)

    with pytest.raises(ValueError, match="collection access mismatch for channel 'systems'"):
        broker.execute({"operation": "verify_access"})


def test_admin_request_rejects_secret_values_and_unknown_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=_FakeAdminBw())

    with pytest.raises(ValueError, match="unsupported Vaultwarden administration request"):
        broker.execute(
            {
                "operation": "create_item",
                "password": "must-not-enter-arguments",  # pragma: allowlist secret
            }
        )


def test_set_item_collections_uses_exact_item_name_and_metadata_only_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=fake)

    result = broker.execute(
        {
            "operation": "set_item_collections",
            "name": "Source",
            "collections": ["finance", "shared"],
        }
    )

    assert result == {"item": "Source", "collections": ["finance", "shared"]}
    assert fake.items["source-id"]["collectionIds"] == [FINANCE_ID, SHARED_ID]
    assert "source-secret" not in json.dumps(result)


def test_upsert_item_clones_existing_login_without_totp_or_secret_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=fake)

    result = broker.execute(
        {
            "operation": "upsert_item",
            "name": "Destination",
            "source_item": "Source",
            "collections": ["finance"],
        }
    )

    assert result == {"created": True, "item": "Destination", "collections": ["finance"]}
    created = fake.items["created-id"]
    assert created["login"] == {
        "username": "person@example.test",
        "password": "source-secret",  # pragma: allowlist secret
        "uris": [{"uri": "https://example.test"}],
    }
    assert all("source-secret" not in " ".join(command) for command, _input in fake.calls)
    assert "source-secret" not in json.dumps(result)


def test_upsert_item_redacts_credentials_and_payload_secrets_from_provider_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    fake.failure_prefix = ("create", "item")
    fake.failure_stderr = "admin@example.test master-password source-secret useful failure"
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=fake)

    with pytest.raises(ValueError, match="useful failure") as raised:
        broker.execute(
            {
                "operation": "upsert_item",
                "name": "Destination",
                "source_item": "Source",
                "collections": ["finance"],
            }
        )

    assert str(raised.value) == "[REDACTED] [REDACTED] [REDACTED] useful failure"


def test_upsert_item_reads_only_mode_0600_protected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    source_dir = tmp_path / "vaultwarden-admin-input"
    source_dir.mkdir()
    source = source_dir / "login.json"
    source.write_text(
        json.dumps({"login": "file-user", "password": "file-secret"}),  # pragma: allowlist secret
        encoding="utf-8",
    )
    source.chmod(0o600)
    fake = _FakeAdminBw()
    fake.items["target-id"] = {"id": "target-id", "name": "Target", "login": {}}
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=fake)

    result = broker.execute(
        {
            "operation": "upsert_item",
            "name": "Target",
            "source_file": "login.json",
            "collections": ["shared"],
        }
    )

    assert result == {"created": False, "item": "Target", "collections": ["shared"]}
    assert fake.items["target-id"]["login"] == {
        "username": "file-user",
        "password": "file-secret",  # pragma: allowlist secret
    }

    source.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        broker.execute(
            {
                "operation": "upsert_item",
                "name": "Target",
                "source_file": "login.json",
                "collections": ["shared"],
            }
        )

    source.unlink()
    source.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(ValueError, match="protected source file"):
        broker.execute(
            {
                "operation": "upsert_item",
                "name": "Target",
                "source_file": "login.json",
                "collections": ["shared"],
            }
        )


def test_create_collection_grants_selected_channel_members_and_updates_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    updates: list[tuple[str, str, tuple[str, ...]]] = []
    runtime = _runtime(
        tmp_path,
        add_collection=lambda alias, identifier, channels: updates.append(
            (alias, identifier, channels)
        ),
    )
    fake = _FakeAdminBw()
    broker = VaultwardenAdminBroker(runtime, run=fake)

    result = broker.execute(
        {
            "operation": "create_collection",
            "alias": "new",
            "name": "New collection",
            "channels": ["finance", "systems"],
        }
    )

    assert result == {"alias": "new", "channels": ["finance", "systems"], "created": True}
    assert updates == [("new", NEW_ID, ("finance", "systems"))]
    assert runtime.collections["new"] == NEW_ID
    assert runtime.channel_collections["finance"] == ("finance", "shared", "new")
    assert runtime.channel_collections["systems"] == ("shared", "new")
    assert fake.org_collections[NEW_ID]["users"] == [
        _grant("finance-member"),
        _grant("systems-member"),
    ]


def test_set_channel_collections_updates_member_grants_and_config_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    updates: list[tuple[str, tuple[str, ...]]] = []
    runtime = _runtime(
        tmp_path,
        update_channel_collections=lambda channel, aliases: updates.append((channel, aliases)),
    )
    fake = _FakeAdminBw()
    broker = VaultwardenAdminBroker(runtime, run=fake)

    result = broker.execute(
        {
            "operation": "set_channel_collections",
            "channel": "finance",
            "collections": ["finance"],
        }
    )

    assert result == {"channel": "finance", "collections": ["finance"]}
    assert updates == [("finance", ("finance",))]
    assert runtime.channel_collections["finance"] == ("finance",)
    assert fake.org_collections[SHARED_ID]["users"] == [
        _grant("external-member"),
        _grant("systems-member"),
    ]


def test_set_channel_collections_rolls_back_grants_when_config_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)

    def fail_update(_channel: str, _aliases: tuple[str, ...]) -> None:
        raise OSError("read-only personalization")

    runtime = _runtime(tmp_path, update_channel_collections=fail_update)
    fake = _FakeAdminBw()
    original = json.loads(json.dumps(fake.org_collections[SHARED_ID]))
    broker = VaultwardenAdminBroker(runtime, run=fake)

    with pytest.raises(OSError, match="read-only personalization"):
        broker.execute(
            {
                "operation": "set_channel_collections",
                "channel": "finance",
                "collections": ["finance"],
            }
        )

    assert fake.org_collections[SHARED_ID] == original
    assert runtime.channel_collections["finance"] == ("finance", "shared")


def test_admin_rejects_missing_members_duplicate_items_and_empty_logins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    fake.members = []
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=fake)
    with pytest.raises(ValueError, match="member is unavailable"):
        broker.execute(
            {
                "operation": "create_collection",
                "alias": "new",
                "name": "New",
                "channels": ["finance"],
            }
        )

    fake = _FakeAdminBw()
    fake.items["one"] = {"id": "one", "name": "Target"}
    fake.items["two"] = {"id": "two", "name": "Target"}
    with pytest.raises(ValueError, match="at most one item"):
        VaultwardenAdminBroker(_runtime(tmp_path), run=fake).execute(
            {
                "operation": "upsert_item",
                "name": "Target",
                "source_item": "Source",
                "collections": ["finance"],
            }
        )

    fake = _FakeAdminBw()
    fake.items["source-id"]["login"] = {}
    with pytest.raises(ValueError, match="no supported login fields"):
        VaultwardenAdminBroker(_runtime(tmp_path), run=fake).execute(
            {
                "operation": "upsert_item",
                "name": "Target",
                "source_item": "Source",
                "collections": ["finance"],
            }
        )


def test_protected_source_rejects_invalid_paths_content_and_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    request = {
        "operation": "upsert_item",
        "name": "Target",
        "source_file": "login.json",
        "collections": ["finance"],
    }
    broker = VaultwardenAdminBroker(_runtime(tmp_path), run=_FakeAdminBw())
    with pytest.raises(ValueError, match="unavailable"):
        broker.execute(request)

    source_dir = tmp_path / "vaultwarden-admin-input"
    source_dir.mkdir()
    source = source_dir / "login.json"
    source.write_text("not-json", encoding="utf-8")
    source.chmod(0o600)
    with pytest.raises(ValueError, match="invalid JSON"):
        broker.execute(request)

    source.write_text("[]", encoding="utf-8")
    with pytest.raises(TypeError, match="JSON object"):
        broker.execute(request)

    source.write_text('{"login":"person"}', encoding="utf-8")
    monkeypatch.setattr(os, "getuid", lambda: source.stat().st_uid + 1)
    with pytest.raises(ValueError, match="wrong owner"):
        broker.execute(request)

    request["source_file"] = "../login.json"
    with pytest.raises(ValueError, match="filename is invalid"):
        broker.execute(request)


@pytest.mark.parametrize(
    ("prefix", "output", "message"),
    [
        (("config", "server"), "https://wrong.example\n", "server does not match"),
        (("status",), "[]", "invalid status"),
        (("status",), "not-json", "invalid JSON"),
        (("unlock",), "", "empty session"),
        (("list", "collections"), "not-json", "invalid JSON"),
    ],
)
def test_admin_cli_states_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: tuple[str, ...],
    output: str,
    message: str,
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    fake.raw_outputs[prefix] = output

    with pytest.raises((TypeError, ValueError), match=message):
        VaultwardenAdminBroker(_runtime(tmp_path), run=fake).execute({"operation": "verify_access"})


def test_admin_cli_login_existing_profile_and_credential_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    fake.raw_outputs["status",] = json.dumps({"status": "unauthenticated"})
    appdata = tmp_path / "vaultwarden-cli" / "finance"
    appdata.mkdir(parents=True)
    (appdata / "data.json").write_text("{}", encoding="utf-8")
    runtime = _runtime(tmp_path)
    runtime.channel_collections["empty"] = ()

    assert (
        VaultwardenAdminBroker(runtime, run=fake).execute({"operation": "verify_access"})[
            "verified"
        ]
        is True
    )
    assert any(command[:1] == ("login",) for command, _input in fake.calls)

    monkeypatch.delenv("PYNCHY_VAULTWARDEN_FINANCE_PASSWORD")
    with pytest.raises(ValueError, match="credentials are unavailable"):
        VaultwardenAdminBroker(_runtime(tmp_path), run=_FakeAdminBw()).execute(
            {"operation": "verify_access"}
        )

    _credentials(monkeypatch)
    monkeypatch.delenv("PYNCHY_VAULTWARDEN_FINANCE_EMAIL")
    with pytest.raises(ValueError, match="credentials are unavailable"):
        VaultwardenAdminBroker(_runtime(tmp_path), run=_FakeAdminBw()).execute(
            {
                "operation": "create_collection",
                "alias": "new",
                "name": "New",
                "channels": ["finance"],
            }
        )


def test_admin_cli_errors_use_safe_fallback_and_nested_nonstring_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    fake.failure_prefix = ("config", "server")
    with pytest.raises(ValueError, match="Bitwarden CLI command failed"):
        VaultwardenAdminBroker(_runtime(tmp_path), run=fake).execute({"operation": "verify_access"})

    fake = _FakeAdminBw()
    fake.items["source-id"]["login"]["uris"] = [{"uri": 7}]
    assert (
        VaultwardenAdminBroker(_runtime(tmp_path), run=fake).execute(
            {
                "operation": "upsert_item",
                "name": "Target",
                "source_item": "Source",
                "collections": ["finance"],
            }
        )["created"]
        is True
    )


@pytest.mark.asyncio
async def test_admin_host_action_rejects_unconfigured_runtime_and_missing_workspace(
    tmp_path: Path,
) -> None:
    action = next(
        item for item in VAULTWARDEN_HOST_ACTIONS.actions if item.tool_name == "manage_vaultwarden"
    )
    configure_vaultwarden_runtime(
        VaultwardenRuntime(
            options=VaultwardenOptions(
                server_url="https://vault.pynchy.svc.cluster.local",
                collections={"finance": FINANCE_ID},
            ),
            data_dir=tmp_path,
            resolve_access=lambda _workspace: None,
        )
    )
    result = await action.handler({"source_group": "secrets", "operation": "verify_access"})
    assert result == {"error": "Vaultwarden administration runtime has not been configured"}

    configure_vaultwarden_runtime(
        VaultwardenRuntime(
            options=VaultwardenOptions(
                server_url="https://vault.pynchy.svc.cluster.local",
                collections={"finance": FINANCE_ID},
            ),
            data_dir=tmp_path,
            resolve_access=lambda _workspace: None,
            admin=_runtime(tmp_path),
        )
    )
    assert await action.handler({"operation": "verify_access"}) == {
        "error": "manage_vaultwarden requires a source workspace"
    }


@pytest.mark.asyncio
@pytest.mark.action("secret.vaultwarden.admin")
async def test_admin_host_action_requires_approval_and_returns_only_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    fake = _FakeAdminBw()
    monkeypatch.setattr(subprocess, "run", fake)
    configure_vaultwarden_runtime(
        VaultwardenRuntime(
            options=VaultwardenOptions(
                server_url="https://vault.pynchy.svc.cluster.local",
                collections={"finance": FINANCE_ID, "shared": SHARED_ID},
            ),
            data_dir=tmp_path,
            resolve_access=lambda _workspace: None,
            admin=_runtime(tmp_path),
        )
    )
    action = next(
        item for item in VAULTWARDEN_HOST_ACTIONS.actions if item.tool_name == "manage_vaultwarden"
    )

    result = await action.handler({"source_group": "secrets", "operation": "verify_access"})

    assert action.access is HostActionAccess.WRITE
    assert action.approval.trigger is ApprovalTrigger.ALWAYS
    assert action.idempotency.mode is IdempotencyMode.IPC_REQUEST_ID
    assert result["result"]["verified"] is True
    assert "master-password" not in json.dumps(result)


@pytest.mark.asyncio
async def test_plugin_composition_persists_collection_and_channel_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    monkeypatch.chdir(tmp_path)
    fake_bw = _FakeAdminBw()
    monkeypatch.setattr(subprocess, "run", fake_bw)
    documents: list[dict[str, Any]] = []

    def fake_mutate(path: Path, mutate: Callable[[tomlkit.TOMLDocument], None]) -> None:
        assert path == tmp_path / "data" / "personalization" / "pynchy.toml"
        document = tomlkit.parse(
            tomlkit.dumps(
                {
                    "connections": {
                        "synapse": {
                            "chat": {
                                "pynchy": {
                                    "channels": {
                                        "finance": {"secret_collections": ["finance", "shared"]},
                                        "systems": (
                                            {"secret_collections": ["shared"]}
                                            if len(documents) < 3
                                            else {}
                                        ),
                                    }
                                }
                            }
                        }
                    },
                    "plugins": {
                        "vaultwarden": {
                            "options": {"collections": {"finance": FINANCE_ID, "shared": SHARED_ID}}
                        }
                    },
                }
            )
        )
        mutate(document)
        documents.append(document.unwrap())

    monkeypatch.setattr(
        "pynchy.host.orchestrator.plugin_configuration.mutate_config_toml", fake_mutate
    )
    settings = Settings.model_validate(
        {
            "connections": {
                "synapse": {
                    "type": "discord",
                    "bot_token_env": "DISCORD_TOKEN",
                    "chat": {
                        "pynchy": {
                            "channels": {
                                "finance": {
                                    "name": "finance",
                                    "secret_collections": ["finance", "shared"],
                                },
                                "systems": {
                                    "name": "systems",
                                    "secret_collections": ["shared"],
                                },
                                "unused": {"name": "unused"},
                            }
                        },
                        "other": {"channels": {"unused": {"name": "unused"}}},
                    },
                },
                "matrix": {"type": "matrix", "expected_user_id": "@owner:example.test"},
            },
            "plugins": {
                "vaultwarden": {
                    "options": {
                        "server_url": "https://vault.pynchy.svc.cluster.local",
                        "collections": {"finance": FINANCE_ID, "shared": SHARED_ID},
                    }
                }
            },
        }
    )
    configure_vaultwarden_plugin(settings)
    action = next(
        item for item in VAULTWARDEN_HOST_ACTIONS.actions if item.tool_name == "manage_vaultwarden"
    )

    await action.handler(
        {
            "source_group": "secrets",
            "operation": "create_collection",
            "alias": "new",
            "name": "New collection",
            "channels": ["finance"],
        }
    )
    await action.handler(
        {
            "source_group": "secrets",
            "operation": "set_channel_collections",
            "channel": "finance",
            "collections": ["finance"],
        }
    )
    await action.handler(
        {
            "source_group": "secrets",
            "operation": "set_channel_collections",
            "channel": "systems",
            "collections": [],
        }
    )
    await action.handler(
        {
            "source_group": "secrets",
            "operation": "set_channel_collections",
            "channel": "systems",
            "collections": [],
        }
    )

    assert documents[0]["plugins"]["vaultwarden"]["options"]["collections"]["new"] == NEW_ID
    assert documents[0]["connections"]["synapse"]["chat"]["pynchy"]["channels"]["finance"][
        "secret_collections"
    ] == ["finance", "shared", "new"]
    assert documents[1]["connections"]["synapse"]["chat"]["pynchy"]["channels"]["finance"][
        "secret_collections"
    ] == ["finance"]
    assert (
        "secret_collections"
        not in documents[2]["connections"]["synapse"]["chat"]["pynchy"]["channels"]["systems"]
    )
