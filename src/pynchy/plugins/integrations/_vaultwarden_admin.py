"""Trusted Vaultwarden administration without secret-valued tool arguments."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import threading
from collections.abc import Callable  # noqa: TC003 - beartype resolves runtime annotations.
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - beartype resolves runtime annotations.
from typing import Any

from ._vaultwarden_admin_cli import AdminBwRunner, BwClient, BwSession, run_bw

_ADMIN_OPERATIONS = frozenset(
    {
        "verify_access",
        "upsert_item",
        "set_item_collections",
        "create_collection",
        "set_channel_collections",
    }
)


@dataclass
class VaultwardenAdminRuntime:
    """Composition-owned state needed by the administration broker."""

    server_url: str
    collections: dict[str, str]
    data_dir: Path
    channel_collections: dict[str, tuple[str, ...]]
    update_channel_collections: Callable[[str, tuple[str, ...]], None]
    add_collection: Callable[[str, str, tuple[str, ...]], None]


@dataclass(frozen=True)
class _GrantContext:
    access: dict[str, tuple[str, ...]]
    member_ids: dict[str, str]
    managed_ids: set[str]
    organization: str


class VaultwardenAdminBroker:
    """Perform bounded vault writes from metadata-only requests."""

    def __init__(self, runtime: VaultwardenAdminRuntime, *, run: AdminBwRunner = run_bw) -> None:
        self.runtime = runtime
        self._client = BwClient(runtime.server_url, runtime.data_dir, run=run)
        self._lock = threading.Lock()

    def execute(self, request: dict[str, Any]) -> dict[str, object]:
        operation = request.get("operation")
        if not isinstance(operation, str) or operation not in _ADMIN_OPERATIONS:
            raise ValueError("unsupported Vaultwarden administration request")
        with self._lock:
            if operation == "verify_access" and set(request) == {"operation"}:
                return self._verify_access()
            if operation == "set_item_collections":
                return self._set_item_collections(request)
            if operation == "upsert_item":
                return self._upsert_item(request)
            if operation == "create_collection":
                return self._create_collection(request)
            if operation == "set_channel_collections":
                return self._set_channel_collections(request)
            raise ValueError("unsupported Vaultwarden administration request")

    def _create_collection(self, request: dict[str, Any]) -> dict[str, object]:
        alias = request.get("alias")
        name = request.get("name")
        channels = request.get("channels")
        if (
            set(request) != {"operation", "alias", "name", "channels"}
            or not isinstance(alias, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", alias) is None
            or alias in self.runtime.collections
            or not isinstance(name, str)
            or not 1 <= len(name) <= 256
            or not isinstance(channels, list)
            or not channels
            or any(not isinstance(channel, str) for channel in channels)
            or len(set(channels)) != len(channels)
            or any(channel not in self.runtime.channel_collections for channel in channels)
        ):
            raise ValueError("invalid Vaultwarden collection creation request")
        with self._client.session("admin") as session:
            organization = self._organization_id(session)
            member_ids = self._channel_member_ids(channels, organization, session)
            payload = {
                "name": name,
                "organizationId": organization,
                "groups": [],
                "users": [_collection_grant(member_ids[channel]) for channel in channels],
            }
            encoded = _encoded(payload)
            created = session.json(
                [
                    "bw",
                    "create",
                    "org-collection",
                    "--organizationid",
                    organization,
                    "--session",
                    session.token,
                ],
                input_value=encoded,
            )
            identifier = created.get("id") if isinstance(created, dict) else None
            if not isinstance(identifier, str):
                raise TypeError("Bitwarden CLI returned an invalid collection")
            try:
                self.runtime.add_collection(alias, identifier, tuple(channels))
            # Rollback must cover failures from the injected config writer.
            except Exception:  # allow: exception-handling
                session.checked(
                    [
                        "bw",
                        "delete",
                        "org-collection",
                        identifier,
                        "--organizationid",
                        organization,
                        "--session",
                        session.token,
                    ],
                )
                raise
            self.runtime.collections[alias] = identifier
            for channel in channels:
                self.runtime.channel_collections[channel] = (
                    *self.runtime.channel_collections[channel],
                    alias,
                )
        return {"alias": alias, "channels": channels, "created": True}

    def _set_channel_collections(self, request: dict[str, Any]) -> dict[str, object]:
        channel = request.get("channel")
        aliases = request.get("collections")
        if (
            set(request) != {"operation", "channel", "collections"}
            or not isinstance(channel, str)
            or channel not in self.runtime.channel_collections
            or not isinstance(aliases, list)
            or any(not isinstance(alias, str) for alias in aliases)
            or len(set(aliases)) != len(aliases)
            or any(alias not in self.runtime.collections for alias in aliases)
        ):
            raise ValueError("invalid Vaultwarden channel collection request")
        old_aliases = self.runtime.channel_collections[channel]
        next_access = {**self.runtime.channel_collections, channel: tuple(aliases)}
        changed_aliases = sorted(set(old_aliases) | set(aliases))
        originals: list[tuple[str, dict[str, Any]]] = []
        with self._client.session("admin") as session:
            organization = self._organization_id(session)
            managed_channels = [name for name, grants in next_access.items() if grants]
            member_ids = self._channel_member_ids(managed_channels, organization, session)
            context = _GrantContext(next_access, member_ids, set(member_ids.values()), organization)
            try:
                originals.extend(
                    self._reconcile_collection_users(alias, context, session)
                    for alias in changed_aliases
                )
                self.runtime.update_channel_collections(channel, tuple(aliases))
            # Restore grants after provider or injected config-writer failures.
            except Exception:  # allow: exception-handling
                for identifier, original in reversed(originals):
                    self._edit_collection(original, identifier, organization, session)
                raise
            self.runtime.channel_collections[channel] = tuple(aliases)
        return {"channel": channel, "collections": aliases}

    def _reconcile_collection_users(
        self,
        alias: str,
        context: _GrantContext,
        session: BwSession,
    ) -> tuple[str, dict[str, Any]]:
        identifier = self.runtime.collections[alias]
        original = self._organization_collection(identifier, context.organization, session)
        desired_ids = {
            context.member_ids[name]
            for name, grants in context.access.items()
            if alias in grants and name in context.member_ids
        }
        users = {
            user["id"]: user
            for user in original.get("users", [])
            if isinstance(user, dict)
            and isinstance(user.get("id"), str)
            and user["id"] not in context.managed_ids
        }
        users.update({identifier: _collection_grant(identifier) for identifier in desired_ids})
        updated = {**original, "users": [users[key] for key in sorted(users)]}
        self._edit_collection(updated, identifier, context.organization, session)
        return identifier, original

    def _channel_member_ids(
        self,
        channels: list[str],
        organization: str,
        session: BwSession,
    ) -> dict[str, str]:
        members = session.json(
            [
                "bw",
                "list",
                "org-members",
                "--organizationid",
                organization,
                "--session",
                session.token,
            ],
        )
        if not isinstance(members, list):
            raise TypeError("Bitwarden CLI returned an invalid organization member list")
        by_email = {
            member["email"].casefold(): member["id"]
            for member in members
            if isinstance(member, dict)
            and isinstance(member.get("email"), str)
            and isinstance(member.get("id"), str)
        }
        result: dict[str, str] = {}
        for channel in channels:
            email = self._client.account_email(channel)
            identifier = by_email.get(email.casefold())
            if identifier is None:
                raise ValueError(f"Vaultwarden member is unavailable for channel {channel!r}")
            result[channel] = identifier
        return result

    def _organization_collection(
        self,
        identifier: str,
        organization: str,
        session: BwSession,
    ) -> dict[str, Any]:
        collection = session.json(
            [
                "bw",
                "get",
                "org-collection",
                identifier,
                "--organizationid",
                organization,
                "--session",
                session.token,
            ],
        )
        if not isinstance(collection, dict):
            raise TypeError("Bitwarden CLI returned an invalid collection")
        return collection

    def _edit_collection(
        self,
        collection: dict[str, Any],
        identifier: str,
        organization: str,
        session: BwSession,
    ) -> None:
        encoded = _encoded(collection)
        session.checked(
            [
                "bw",
                "edit",
                "org-collection",
                identifier,
                "--organizationid",
                organization,
                "--session",
                session.token,
            ],
            input_value=encoded,
        )

    def _set_item_collections(self, request: dict[str, Any]) -> dict[str, object]:
        name, aliases = self._item_request(request, source_keys=())
        with self._client.session("admin") as session:
            item = self._one_exact_item(name, session)
            identifiers = [self.runtime.collections[alias] for alias in aliases]
            encoded = _encoded(identifiers)
            session.checked(
                [
                    "bw",
                    "edit",
                    "item-collections",
                    item["id"],
                    "--session",
                    session.token,
                ],
                input_value=encoded,
            )
        return {"item": name, "collections": aliases}

    def _upsert_item(self, request: dict[str, Any]) -> dict[str, object]:
        sources = tuple(key for key in ("source_item", "source_file") if key in request)
        if len(sources) != 1:
            raise ValueError("upsert_item requires exactly one protected source")
        name, aliases = self._item_request(request, source_keys=sources)
        source_name = request[sources[0]]
        if not isinstance(source_name, str) or not source_name:
            raise ValueError("upsert_item source is invalid")
        with self._client.session("admin") as session:
            if sources[0] == "source_item":
                source = self._one_exact_item(source_name, session)
            else:
                source = self._protected_item(source_name)
            target = self._exact_items(name, session)
            if len(target) > 1:
                raise ValueError(f"expected at most one item named {name!r}; found {len(target)}")
            organization = self._organization_id(session)
            payload = {
                "type": 1,
                "name": name,
                "organizationId": organization,
                "collectionIds": [self.runtime.collections[alias] for alias in aliases],
                "login": _login_payload(source),
            }
            encoded = _encoded(payload)
            existing = next(iter(target.values()), None)
            command = ["bw", "create", "item"]
            if existing is not None:
                command = ["bw", "edit", "item", existing["id"]]
            session.checked(
                [*command, "--session", session.token],
                input_value=encoded,
                sensitive_values=tuple(_string_values(payload["login"])),
            )
        return {"created": existing is None, "item": name, "collections": aliases}

    def _item_request(
        self, request: dict[str, Any], *, source_keys: tuple[str, ...]
    ) -> tuple[str, list[str]]:
        expected = {"operation", "name", "collections", *source_keys}
        name = request.get("name")
        aliases = request.get("collections")
        if (
            set(request) != expected
            or not isinstance(name, str)
            or not 1 <= len(name) <= 256
            or not isinstance(aliases, list)
            or not aliases
            or any(not isinstance(alias, str) for alias in aliases)
            or len(set(aliases)) != len(aliases)
        ):
            raise ValueError("invalid Vaultwarden item administration request")
        unknown = [alias for alias in aliases if alias not in self.runtime.collections]
        if unknown:
            raise ValueError(f"unknown Vaultwarden collection: {unknown[0]}")
        return name, aliases

    def _one_exact_item(
        self,
        name: str,
        session: BwSession,
    ) -> dict[str, Any]:
        matches = self._exact_items(name, session)
        if len(matches) != 1:
            raise ValueError(f"expected exactly one item named {name!r}; found {len(matches)}")
        return next(iter(matches.values()))

    def _exact_items(
        self,
        name: str,
        session: BwSession,
    ) -> dict[str, dict[str, Any]]:
        items = session.json(
            ["bw", "list", "items", "--search", name, "--session", session.token],
        )
        if not isinstance(items, list):
            raise TypeError("Bitwarden CLI returned an invalid item list")
        return {
            item["id"]: item
            for item in items
            if isinstance(item, dict)
            and item.get("name") == name
            and isinstance(item.get("id"), str)
        }

    def _organization_id(self, session: BwSession) -> str:
        organizations = session.json(
            ["bw", "list", "organizations", "--session", session.token],
        )
        identifiers: list[str] = []
        if isinstance(organizations, list):
            for item in organizations:
                identifier = item.get("id") if isinstance(item, dict) else None
                if isinstance(identifier, str):
                    identifiers.append(identifier)
        if len(identifiers) != 1:
            raise ValueError("Vaultwarden administrator must belong to exactly one organization")
        return identifiers[0]

    def _protected_item(self, filename: str) -> dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", filename) is None:
            raise ValueError("protected source filename is invalid")
        directory = self.runtime.data_dir / "vaultwarden-admin-input"
        item = _read_protected_json(directory, filename)
        if not isinstance(item, dict):
            raise TypeError("protected source file must contain a JSON object")
        return item

    def _verify_access(self) -> dict[str, object]:
        verified: dict[str, list[str]] = {}
        for channel, aliases in sorted(self.runtime.channel_collections.items()):
            if not aliases:
                continue
            with self._client.session(channel) as session:
                collections = session.json(
                    ["bw", "list", "collections", "--session", session.token],
                )
            if not isinstance(collections, list):
                raise TypeError("Bitwarden CLI returned an invalid collection list")
            visible = {
                item["id"]
                for item in collections
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            expected = {self.runtime.collections[alias] for alias in aliases}
            if visible != expected:
                raise ValueError(f"collection access mismatch for channel {channel!r}")
            verified[channel] = list(aliases)
        return {"channels": verified, "verified": True}


def _read_protected_json(directory: Path, filename: str) -> object:
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("protected source file is unavailable") from exc
    try:
        try:
            source_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except OSError as exc:
            raise ValueError("protected source file is unavailable") from exc
    finally:
        os.close(directory_fd)
    with os.fdopen(source_fd, encoding="utf-8") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("protected source file must be a regular file with mode 0600")
        if metadata.st_uid != os.getuid():
            raise ValueError("protected source file has the wrong owner")
        try:
            return json.load(source)
        except json.JSONDecodeError as exc:
            raise ValueError("protected source file contains invalid JSON") from exc


def _encoded(value: object) -> str:
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _collection_grant(identifier: str) -> dict[str, object]:
    return {"id": identifier, "readOnly": False, "hidePasswords": False, "manage": False}


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    return []


def _login_payload(item: dict[str, Any]) -> dict[str, object]:
    raw_login = item.get("login")
    if isinstance(raw_login, dict):
        login = {
            key: raw_login[key] for key in ("username", "password", "uris") if raw_login.get(key)
        }
    else:
        username = raw_login if isinstance(raw_login, str) else item.get("email")
        login = {
            key: value
            for key, value in (("username", username), ("password", item.get("password")))
            if isinstance(value, str) and value
        }
    if not login:
        raise ValueError("source contains no supported login fields")
    return login
