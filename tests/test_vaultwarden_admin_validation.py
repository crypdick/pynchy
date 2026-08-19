"""Validation edges for Vaultwarden administration requests and provider data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from vaultwarden_admin_test_support import (
    NEW_ID,
    FakeAdminBw,
    admin_runtime,
    set_admin_credentials,
)

from pynchy.plugins.integrations.vaultwarden import VaultwardenAdminBroker


def test_create_collection_rolls_back_provider_when_config_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_admin_credentials(monkeypatch)

    def fail_update(_alias: str, _identifier: str, _channels: tuple[str, ...]) -> None:
        raise OSError("read-only personalization")

    fake = FakeAdminBw()
    broker = VaultwardenAdminBroker(admin_runtime(tmp_path, add_collection=fail_update), run=fake)

    with pytest.raises(OSError, match="read-only personalization"):
        broker.execute(
            {
                "operation": "create_collection",
                "alias": "new",
                "name": "New collection",
                "channels": ["finance"],
            }
        )

    assert NEW_ID not in fake.org_collections


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"operation": "verify_access", "extra": True}, "unsupported"),
        ({"operation": "create_collection", "alias": "bad alias"}, "collection creation"),
        ({"operation": "set_channel_collections", "channel": "missing"}, "channel collection"),
        (
            {"operation": "upsert_item", "name": "Target", "collections": ["shared"]},
            "one protected",
        ),
        (
            {
                "operation": "upsert_item",
                "name": "Target",
                "collections": ["shared"],
                "source_item": 7,
            },
            "source is invalid",
        ),
        (
            {"operation": "set_item_collections", "name": "", "collections": []},
            "item administration",
        ),
        (
            {"operation": "set_item_collections", "name": "Source", "collections": ["missing"]},
            "unknown Vaultwarden collection",
        ),
    ],
)
def test_admin_requests_fail_closed_before_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    message: str,
) -> None:
    set_admin_credentials(monkeypatch)

    with pytest.raises(ValueError, match=message):
        VaultwardenAdminBroker(admin_runtime(tmp_path), run=FakeAdminBw()).execute(payload)


@pytest.mark.parametrize(
    ("prefix", "output", "operation", "message"),
    [
        (
            ("create", "org-collection"),
            "[]",
            {
                "operation": "create_collection",
                "alias": "new",
                "name": "New",
                "channels": ["finance"],
            },
            "invalid collection",
        ),
        (
            ("list", "org-members"),
            "{}",
            {
                "operation": "create_collection",
                "alias": "new",
                "name": "New",
                "channels": ["finance"],
            },
            "invalid organization member list",
        ),
        (
            ("get", "org-collection"),
            "[]",
            {
                "operation": "set_channel_collections",
                "channel": "finance",
                "collections": ["finance"],
            },
            "invalid collection",
        ),
        (
            ("list", "items"),
            "{}",
            {"operation": "set_item_collections", "name": "Source", "collections": ["finance"]},
            "invalid item list",
        ),
        (
            ("list", "items"),
            "[]",
            {"operation": "set_item_collections", "name": "Missing", "collections": ["finance"]},
            "exactly one item",
        ),
        (
            ("list", "collections"),
            "{}",
            {"operation": "verify_access"},
            "invalid collection list",
        ),
    ],
)
def test_admin_provider_shapes_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: tuple[str, ...],
    output: str,
    operation: dict[str, Any],
    message: str,
) -> None:
    set_admin_credentials(monkeypatch)
    fake = FakeAdminBw()
    fake.raw_outputs[prefix] = output

    with pytest.raises((TypeError, ValueError), match=message):
        VaultwardenAdminBroker(admin_runtime(tmp_path), run=fake).execute(operation)


@pytest.mark.parametrize(
    "organizations", ["{}", '[{}, {"id": 7}]', '[{"id": "one"}, {"id": "two"}]']
)
def test_admin_requires_exactly_one_valid_organization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, organizations: str
) -> None:
    set_admin_credentials(monkeypatch)
    fake = FakeAdminBw()
    fake.raw_outputs["list", "organizations"] = organizations

    with pytest.raises(ValueError, match="exactly one organization"):
        VaultwardenAdminBroker(admin_runtime(tmp_path), run=fake).execute(
            {
                "operation": "create_collection",
                "alias": "new",
                "name": "New",
                "channels": ["finance"],
            }
        )
