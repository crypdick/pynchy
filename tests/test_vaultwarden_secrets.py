"""Channel-scoped Vaultwarden access never exposes secret values through IPC."""

from __future__ import annotations

import json
import stat
import subprocess  # noqa: S404 - fake CompletedProcess values only.
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pynchy.config.api import Settings
from pynchy.host.container_manager.ipc.write import clean_secret_files, configure_ipc_base_dir
from pynchy.host.orchestrator.plugin_configuration import configure_vaultwarden_plugin
from pynchy.plugins.integrations.vaultwarden import (
    VAULTWARDEN_HOST_ACTIONS,
    VaultwardenBroker,
    VaultwardenOptions,
    VaultwardenRuntime,
    configure_vaultwarden_runtime,
)

COLLECTION_ID = "11111111-1111-4111-8111-111111111111"
SHARED_ID = "22222222-2222-4222-8222-222222222222"


def _settings_data(*, collections: list[str]) -> dict[str, Any]:
    return {
        "connections": {
            "synapse": {
                "type": "discord",
                "bot_token_env": "DISCORD_TOKEN",
                "chat": {
                    "pynchy": {
                        "channels": {
                            "finance": {
                                "name": "finance",
                                "secret_collections": collections,
                            },
                            "general": {"name": "general"},
                        }
                    }
                },
            }
        },
        "plugins": {
            "vaultwarden": {
                "options": {
                    "server_url": "https://vault.example.test",
                    "collections": {
                        "finance": COLLECTION_ID,
                        "shared": SHARED_ID,
                    },
                }
            }
        },
        "profiles": {"base": {}},
        "workspaces": {
            "finance": {
                "profiles": ["base"],
                "chat": "connection.discord.synapse.chat.pynchy.channels.finance",
                "threads": [
                    {
                        "name": "banking",
                        "workspace": "banking",
                        "profiles": ["base"],
                    }
                ],
            },
            "general": {
                "profiles": ["base"],
                "chat": "connection.discord.synapse.chat.pynchy.channels.general",
            },
        },
    }


def _settings(*, collections: list[str]) -> Settings:
    return Settings.model_validate(_settings_data(collections=collections))


def test_channel_collections_enable_secret_access_for_all_child_workspaces() -> None:
    settings = _settings(collections=["finance", "shared"])

    assert settings.secret_collections_for_workspace("finance") == ("finance", "shared")
    assert settings.secret_collections_for_workspace("finance__thread_discord-channel-123") == (
        "finance",
        "shared",
    )
    assert settings.secret_collections_for_workspace("banking") == ("finance", "shared")
    assert settings.secret_collections_for_workspace("banking__thread_discord-channel-456") == (
        "finance",
        "shared",
    )
    assert settings.secret_collections_for_workspace("general") == ()
    assert settings.resolved_workspace_config("finance").tools == [
        "vaultwarden",
        "vaultwarden-browser.finance",
    ]
    assert settings.resolved_workspace_config("finance").contains_secrets is True
    assert settings.resolved_workspace_config("general").tools == []
    browser = settings.tools["vaultwarden-browser.finance"]
    assert browser.type == "mcp"
    assert browser.mcp.idle_timeout == 0
    assert "vaultwarden-finance" in browser.mcp.args[0]


def test_unknown_channel_collection_fails_closed() -> None:
    with pytest.raises(ValidationError, match="unknown Vaultwarden collection: missing"):
        _settings(collections=["missing"])


def test_channel_collection_requires_an_enabled_plugin() -> None:
    data = _settings_data(collections=["finance"])
    data["plugins"]["vaultwarden"]["enabled"] = False

    with pytest.raises(ValidationError, match="requires the vaultwarden plugin"):
        Settings.model_validate(data)


@pytest.mark.parametrize("reserved_tool", ["vaultwarden", "vaultwarden-browser.finance"])
def test_channel_collection_rejects_reserved_tool_overrides(reserved_tool: str) -> None:
    data = _settings_data(collections=["finance"])
    data["tools"] = {reserved_tool: {"type": "builtin"}}

    with pytest.raises(ValidationError, match="reserved for channel-scoped"):
        Settings.model_validate(data)


def test_secret_enabled_channel_keys_must_be_globally_unique() -> None:
    data = _settings_data(collections=["finance"])
    data["connections"]["second"] = {
        "type": "discord",
        "bot_token_env": "SECOND_DISCORD_TOKEN",
        "chat": {
            "other": {
                "channels": {"finance": {"name": "finance", "secret_collections": ["finance"]}}
            }
        },
    }

    with pytest.raises(ValidationError, match="channel name is ambiguous: finance"):
        Settings.model_validate(data)


def test_malformed_parent_channel_reference_has_no_secret_access() -> None:
    settings = _settings(collections=["finance"])
    settings.workspaces["finance"].chat = "connection.discord.synapse.chat.pynchy"

    assert settings.secret_access_for_workspace("finance") is None


def test_empty_vaultwarden_plugin_options_need_no_runtime_configuration() -> None:
    data = _settings_data(collections=[])
    data["plugins"]["vaultwarden"]["options"] = {}

    configure_vaultwarden_plugin(Settings.model_validate(data))


@pytest.mark.parametrize(
    "server_url",
    [
        "http://vault.example.test",
        "https://user@vault.example.test",
        "https://vault.example.test?query=yes",
        "https://vault.example.test#fragment",
    ],
)
def test_vaultwarden_server_requires_a_plain_https_origin(server_url: str) -> None:
    with pytest.raises(ValidationError, match="Vaultwarden server_url"):
        VaultwardenOptions(server_url=server_url, collections={"finance": COLLECTION_ID})


class _FakeBw:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.server = "https://vault.example.test"
        self.status: object = {"status": "locked"}
        self.status_output: str | None = None
        self.unlock_output = "session-key\n"
        self.list_output: object | None = None
        self.fail_command: str | None = None
        self.failure_stderr = ""

    def __call__(  # noqa: PLR0911 - small fake dispatches fixed CLI commands.
        self,
        args: list[str],
        *,
        env: dict[str, str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del env
        command = tuple(args[1:])
        self.calls.append(command)
        if self.fail_command is not None and command[0] == self.fail_command:
            return subprocess.CompletedProcess(args, 1, "", self.failure_stderr)
        if command[:2] == ("config", "server"):
            return subprocess.CompletedProcess(args, 0, f"{self.server}\n", "")
        if command[0] == "status":
            output = (
                self.status_output if self.status_output is not None else json.dumps(self.status)
            )
            return subprocess.CompletedProcess(args, 0, output, "")
        if command[0] == "login":
            return subprocess.CompletedProcess(args, 0, "", "")
        if command[0] == "unlock":
            return subprocess.CompletedProcess(args, 0, self.unlock_output, "")
        if command[0] in {"sync", "lock"}:
            return subprocess.CompletedProcess(args, 0, "", "")
        if command[:2] == ("list", "items"):
            if self.list_output is not None:
                return subprocess.CompletedProcess(args, 0, json.dumps(self.list_output), "")
            collection_id = command[command.index("--collectionid") + 1]
            items = []
            if collection_id == COLLECTION_ID:
                items = [
                    {
                        "id": "item-1",
                        "name": "Example",
                        "login": {
                            "username": "person@example.test",
                            "password": "never-in-result",  # pragma: allowlist secret
                            "totp": "never-export-totp",
                        },
                    }
                ]
            return subprocess.CompletedProcess(args, 0, json.dumps(items), "")
        raise AssertionError(command)


def _broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bw: _FakeBw,
    *,
    access: tuple[str, tuple[str, ...]] | None = ("finance", ("finance",)),
) -> VaultwardenBroker:
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_CLIENTID", "client-id")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_CLIENTSECRET", "client-secret")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_PASSWORD", "master-password")
    return VaultwardenBroker(
        VaultwardenRuntime(
            options=VaultwardenOptions(
                server_url="https://vault.example.test",
                collections={"finance": COLLECTION_ID, "shared": SHARED_ID},
            ),
            data_dir=tmp_path,
            resolve_access=lambda _workspace: access,
        ),
        run=fake_bw,
    )


@pytest.mark.action("secret.vaultwarden.read")
def test_get_secret_searches_only_granted_collections_and_writes_mode_0600(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appdata = tmp_path / "vaultwarden-cli" / "finance"
    appdata.mkdir(parents=True)
    (appdata / "data.json").write_text("{}")
    fake_bw = _FakeBw()
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_CLIENTID", "client-id")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_CLIENTSECRET", "client-secret")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_PASSWORD", "master-password")
    broker = VaultwardenBroker(
        VaultwardenRuntime(
            options=VaultwardenOptions(
                server_url="https://vault.example.test",
                collections={"finance": COLLECTION_ID, "shared": SHARED_ID},
            ),
            data_dir=tmp_path,
            resolve_access=lambda _workspace: ("finance", ("finance", "shared")),
        ),
        run=fake_bw,
    )

    result = broker.get_secret("finance", "Example")

    assert result["keys"] == ["email", "login", "password"]
    assert result["path"].startswith("/tmp/pynchy-secrets/")  # noqa: S108 - public contract.
    assert "never-in-result" not in json.dumps(result)
    host_path = tmp_path / "ipc" / "finance" / "secrets" / Path(result["path"]).name
    assert stat.S_IMODE(host_path.stat().st_mode) == 0o600
    assert json.loads(host_path.read_text()) == {
        "email": "person@example.test",
        "login": "person@example.test",
        "password": "never-in-result",  # pragma: allowlist secret
    }
    list_calls = [call for call in fake_bw.calls if call[:2] == ("list", "items")]
    assert [call[call.index("--collectionid") + 1] for call in list_calls] == [
        COLLECTION_ID,
        SHARED_ID,
    ]


def test_get_secret_rejects_ambiguous_exact_names(tmp_path: Path, monkeypatch) -> None:
    fake_bw = _FakeBw()
    original = fake_bw.__call__

    def duplicate(args, *, env, input_value=None):
        del input_value
        result = original(args, env=env)
        command = tuple(args[1:])
        if command[:2] == ("list", "items"):
            item = {
                "id": command[command.index("--collectionid") + 1],
                "name": "Example",
                "login": {"password": "hidden"},  # pragma: allowlist secret
            }
            return subprocess.CompletedProcess(args, 0, json.dumps([item]), "")
        return result

    appdata = tmp_path / "vaultwarden-cli" / "finance"
    appdata.mkdir(parents=True)
    (appdata / "data.json").write_text("{}")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_CLIENTID", "id")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_CLIENTSECRET", "secret")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_PASSWORD", "password")
    broker = VaultwardenBroker(
        VaultwardenRuntime(
            options=VaultwardenOptions(
                server_url="https://vault.example.test",
                collections={"finance": COLLECTION_ID, "shared": SHARED_ID},
            ),
            data_dir=tmp_path,
            resolve_access=lambda _workspace: ("finance", ("finance", "shared")),
        ),
        run=duplicate,
    )

    with pytest.raises(ValueError, match="exactly one item named 'Example'; found 2"):
        broker.get_secret("finance", "Example")


def test_secret_files_are_removed_at_runtime_cleanup(tmp_path: Path) -> None:
    secret_dir = tmp_path / "finance" / "secrets"
    secret_dir.mkdir(parents=True)
    secret = secret_dir / "secret.json"
    secret.write_text("sensitive")
    configure_ipc_base_dir(tmp_path)

    clean_secret_files("finance")

    assert not secret.exists()


def test_secret_cleanup_without_a_runtime_is_a_noop() -> None:
    clean_secret_files(None)


@pytest.mark.parametrize(
    ("name", "access", "message"),
    [
        ("", ("finance", ("finance",)), "1 to 256"),
        ("Example", None, "not enabled"),
        ("Example", ("bad account", ("finance",)), "account name is invalid"),
    ],
)
def test_get_secret_rejects_invalid_requests_before_calling_bw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    access: tuple[str, tuple[str, ...]] | None,
    message: str,
) -> None:
    fake_bw = _FakeBw()
    broker = _broker(tmp_path, monkeypatch, fake_bw, access=access)

    with pytest.raises((ValueError, PermissionError), match=message):
        broker.get_secret("finance", name)

    assert fake_bw.calls == []


def test_get_secret_initializes_and_logs_in_a_new_cli_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bw = _FakeBw()
    fake_bw.status = {"status": "unauthenticated"}
    broker = _broker(tmp_path, monkeypatch, fake_bw)

    result = broker.get_secret("finance", "Example")

    assert result["keys"] == ["email", "login", "password"]
    assert ("config", "server", "https://vault.example.test") in fake_bw.calls
    assert ("login", "--apikey") in fake_bw.calls


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda fake: setattr(fake, "server", "https://wrong.example.test"), "does not match"),
        (lambda fake: setattr(fake, "status", []), "invalid status"),
        (lambda fake: setattr(fake, "status_output", "not-json"), "invalid JSON"),
        (lambda fake: setattr(fake, "unlock_output", ""), "empty session"),
        (lambda fake: setattr(fake, "list_output", {}), "invalid item list"),
        (
            lambda fake: setattr(
                fake,
                "list_output",
                [None, {"name": "Other"}, {"name": "Example", "id": None}],
            ),
            "found 0",
        ),
        (
            lambda fake: setattr(
                fake,
                "list_output",
                [{"id": "item", "name": "Example", "login": "invalid"}],
            ),
            "no supported login fields",
        ),
    ],
)
def test_get_secret_fails_closed_on_invalid_provider_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup,
    message: str,
) -> None:
    fake_bw = _FakeBw()
    setup(fake_bw)
    appdata = tmp_path / "vaultwarden-cli" / "finance"
    appdata.mkdir(parents=True)
    (appdata / "data.json").write_text("{}")
    broker = _broker(tmp_path, monkeypatch, fake_bw)

    with pytest.raises((TypeError, ValueError), match=message):
        broker.get_secret("finance", "Example")


def test_get_secret_requires_all_account_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(tmp_path, monkeypatch, _FakeBw())
    monkeypatch.delenv("PYNCHY_VAULTWARDEN_FINANCE_PASSWORD")

    with pytest.raises(ValueError, match="credentials are unavailable"):
        broker.get_secret("finance", "Example")


def test_bw_errors_redact_credentials_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bw = _FakeBw()
    fake_bw.fail_command = "sync"
    fake_bw.failure_stderr = "client-secret master-password session-key useful failure"
    broker = _broker(tmp_path, monkeypatch, fake_bw)

    with pytest.raises(ValueError, match="useful failure") as raised:
        broker.get_secret("finance", "Example")

    message = str(raised.value)
    assert message == "[REDACTED] [REDACTED] [REDACTED] useful failure"


@pytest.mark.parametrize(
    ("login", "expected_keys"),
    [
        (
            {"username": "local-user", "password": "value"},  # pragma: allowlist secret
            ["login", "password"],
        ),
        ({"username": 7, "password": "value"}, ["password"]),  # pragma: allowlist secret
        (
            {"username": "person@example.test", "password": None},  # pragma: allowlist secret
            ["email", "login"],
        ),
    ],
)
def test_get_secret_normalizes_only_supported_nonempty_login_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login: dict[str, object],
    expected_keys: list[str],
) -> None:
    fake_bw = _FakeBw()
    fake_bw.list_output = [{"id": "item", "name": "Example", "login": login}]
    broker = _broker(tmp_path, monkeypatch, fake_bw)

    assert broker.get_secret("finance", "Example")["keys"] == expected_keys


@pytest.mark.asyncio
async def test_vaultwarden_service_handler_validates_and_delegates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = VAULTWARDEN_HOST_ACTIONS.actions[0].handler
    assert await handler({"source_group": "finance", "name": "Example"}) == {
        "error": "Vaultwarden runtime has not been configured"
    }
    fake_bw = _FakeBw()
    monkeypatch.setattr(subprocess, "run", fake_bw)
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_CLIENTID", "client-id")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_CLIENTSECRET", "client-secret")
    monkeypatch.setenv("PYNCHY_VAULTWARDEN_FINANCE_PASSWORD", "master-password")
    configure_vaultwarden_runtime(
        VaultwardenRuntime(
            options=VaultwardenOptions(
                server_url="https://vault.example.test",
                collections={"finance": COLLECTION_ID},
            ),
            data_dir=tmp_path,
            resolve_access=lambda _workspace: ("finance", ("finance",)),
        )
    )

    assert await handler({"source_group": 7, "name": "Example"}) == {
        "error": "get_secret requires a secret name"
    }
    result = await handler({"source_group": "finance", "name": "Example"})
    assert result["result"]["keys"] == ["email", "login", "password"]
    configure_vaultwarden_plugin(_settings(collections=["finance"]))
