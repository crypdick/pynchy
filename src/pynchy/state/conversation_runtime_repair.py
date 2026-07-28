"""Repair routed-conversation runtime ownership projections."""

from __future__ import annotations

from dataclasses import dataclass

from aiosqlite import (  # noqa: TC002, RUF100 - beartype resolves recovery annotations at runtime.
    Connection,
)

from pynchy.conversation.api import (
    ConversationId,
    conversation_id_from_folder,
    dynamic_thread_folder,
    parent_workspace_name,
    routed_conversation_folder,
)
from pynchy.logger import logger
from pynchy.types import ChatJid, GroupFolder, SessionId


class RuntimeOwnershipRepairConflictError(RuntimeError):
    """A corrupt owner cannot be repaired without stealing another runtime."""


@dataclass(frozen=True)
class _RuntimeOwnershipEvidence:
    """Durable evidence from which one repair plan is derived."""

    conversation_id: ConversationId
    thread_jid: ChatJid
    session_id: SessionId | None
    original_workspace: GroupFolder
    parent_workspace: GroupFolder
    profile_folder: GroupFolder | None
    delivery_workspace: GroupFolder | None


@dataclass(frozen=True)
class _RuntimeOwnershipRepair:
    """One conversation's validated target and movable runtime projections."""

    conversation_id: ConversationId
    thread_jid: ChatJid
    session_id: SessionId | None
    original_workspace: GroupFolder
    target_workspace: GroupFolder
    routed_folder: GroupFolder
    source_folders: tuple[GroupFolder, ...]
    profile_folder: GroupFolder | None

    @property
    def repairs_owner(self) -> bool:
        return self.target_workspace != self.original_workspace

    @property
    def profile_source(self) -> GroupFolder | None:
        folder = self.profile_folder
        if folder is None:
            return None
        if (
            folder in self.source_folders
            or conversation_id_from_folder(folder) == self.conversation_id
        ):
            return folder
        return None


def _runtime_source_folders(
    conversation_id: ConversationId,
    thread_jid: ChatJid,
    original_workspace: GroupFolder,
    target_workspace: GroupFolder,
    profile_folder: GroupFolder | None,
) -> tuple[GroupFolder, ...]:
    """Return only folders that can safely belong to this conversation."""
    candidates = [
        GroupFolder(dynamic_thread_folder(original_workspace, thread_jid)),
        GroupFolder(routed_conversation_folder(original_workspace, conversation_id)),
        GroupFolder(dynamic_thread_folder(target_workspace, thread_jid)),
    ]
    if profile_folder is not None and (
        profile_folder in candidates
        or conversation_id_from_folder(profile_folder) == conversation_id
    ):
        candidates.append(profile_folder)
    target_folder = GroupFolder(routed_conversation_folder(target_workspace, conversation_id))
    return tuple(dict.fromkeys(folder for folder in candidates if folder != target_folder))


def _plan_runtime_ownership_repair(
    evidence: _RuntimeOwnershipEvidence,
    *,
    allow_owner_reassignment: bool,
) -> _RuntimeOwnershipRepair:
    """Derive a repair without mutating any durable projection."""
    profile_workspace_name = (
        parent_workspace_name(evidence.profile_folder)
        if evidence.profile_folder is not None
        and conversation_id_from_folder(evidence.profile_folder) == evidence.conversation_id
        else None
    )
    profile_workspace = (
        GroupFolder(profile_workspace_name) if profile_workspace_name is not None else None
    )
    # The authenticated receipt records the route-selected owner before a
    # control surface can accidentally overwrite either mutable projection.
    authoritative_workspace = evidence.delivery_workspace or profile_workspace
    should_repair_owner = (
        allow_owner_reassignment
        and authoritative_workspace is not None
        and authoritative_workspace != evidence.original_workspace
        and evidence.original_workspace == evidence.parent_workspace
    )
    target_workspace = evidence.original_workspace
    if should_repair_owner and authoritative_workspace is not None:
        target_workspace = authoritative_workspace
    return _RuntimeOwnershipRepair(
        conversation_id=evidence.conversation_id,
        thread_jid=evidence.thread_jid,
        session_id=evidence.session_id,
        original_workspace=evidence.original_workspace,
        target_workspace=target_workspace,
        routed_folder=GroupFolder(
            routed_conversation_folder(
                target_workspace,
                evidence.conversation_id,
            )
        ),
        source_folders=_runtime_source_folders(
            evidence.conversation_id,
            evidence.thread_jid,
            evidence.original_workspace,
            target_workspace,
            evidence.profile_folder,
        ),
        profile_folder=evidence.profile_folder,
    )


async def repair_conversation_runtime_ownership(
    database: Connection,
    *,
    allow_owner_reassignment: bool,
) -> int:
    """Collapse thread-derived runtimes into their routed conversation owner."""
    cursor = await database.execute(
        """
        SELECT conversation.id, conversation.workspace, conversation.session_id,
               binding.parent_workspace, binding.thread_jid, profile.folder AS profile_folder,
               (
                   SELECT receipt.workspace
                   FROM conversation_deliveries AS delivery
                   JOIN webhook_receipts AS receipt
                     ON receipt.provider = delivery.provider
                    AND receipt.route = delivery.route
                    AND receipt.delivery_id = delivery.delivery_id
                   WHERE delivery.conversation_id = conversation.id
                   ORDER BY delivery.sequence DESC
                   LIMIT 1
               ) AS delivery_workspace
        FROM routed_conversations AS conversation
        JOIN conversation_control_bindings AS binding
          ON binding.conversation_id = conversation.id
        LEFT JOIN registered_groups AS profile
          ON profile.jid = binding.thread_jid
        """
    )
    migrated = 0
    for row in await cursor.fetchall():
        plan = _plan_runtime_ownership_repair(
            _RuntimeOwnershipEvidence(
                conversation_id=ConversationId(row["id"]),
                thread_jid=ChatJid(str(row["thread_jid"])),
                session_id=(
                    SessionId(str(row["session_id"])) if row["session_id"] is not None else None
                ),
                original_workspace=GroupFolder(str(row["workspace"])),
                parent_workspace=GroupFolder(str(row["parent_workspace"])),
                profile_folder=(
                    GroupFolder(str(row["profile_folder"]))
                    if row["profile_folder"] is not None
                    else None
                ),
                delivery_workspace=(
                    GroupFolder(str(row["delivery_workspace"]))
                    if row["delivery_workspace"] is not None
                    else None
                ),
            ),
            allow_owner_reassignment=allow_owner_reassignment,
        )
        if not await _repair_target_is_available(database, plan):
            continue
        migrated += await _apply_runtime_ownership_repair(database, plan)
    return migrated


async def _repair_target_is_available(
    database: Connection,
    plan: _RuntimeOwnershipRepair,
) -> bool:
    """Fail closed when another JID already owns the planned target."""
    target_cursor = await database.execute(
        "SELECT jid FROM registered_groups WHERE folder = ?",
        (plan.routed_folder,),
    )
    target = await target_cursor.fetchone()
    if target is not None and target["jid"] != plan.thread_jid:
        message = (
            f"Routed workspace target {plan.routed_folder} is owned by "
            f"{target['jid']}, not {plan.thread_jid}"
        )
        if plan.repairs_owner:
            raise RuntimeOwnershipRepairConflictError(message)
        logger.warning("Legacy routed workspace ownership migration blocked", reason=message)
        return False
    if plan.session_id is not None:
        session_cursor = await database.execute(
            "SELECT session_id FROM sessions WHERE group_folder = ?",
            (plan.routed_folder,),
        )
        target_session = await session_cursor.fetchone()
        if target_session is not None and target_session["session_id"] != plan.session_id:
            target_session_id = SessionId(str(target_session["session_id"]))
            message = (
                f"Routed session target {plan.routed_folder} contains "
                f"{target_session_id}, not {plan.session_id}"
            )
            if not plan.repairs_owner:
                logger.warning("Legacy routed session migration blocked", reason=message)
                return False
            if await _target_session_has_other_owners(database, plan, target_session_id):
                raise RuntimeOwnershipRepairConflictError(message)
            logger.warning(
                "Replacing stale routed session during authenticated owner repair",
                conversation_id=plan.conversation_id,
                group_folder=plan.routed_folder,
                stale_session_id=target_session_id,
            )
    return await _repair_sources_are_available(database, plan)


async def _target_session_has_other_owners(
    database: Connection,
    plan: _RuntimeOwnershipRepair,
    target_session_id: SessionId,
) -> bool:
    """Return whether replacing a stale target session could steal live state."""
    cursor = await database.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM routed_conversations
            WHERE session_id = ? AND id != ?
            UNION ALL
            SELECT 1
            FROM in_flight_turns
            WHERE session_id = ?
            UNION ALL
            SELECT 1
            FROM sessions
            WHERE session_id = ? AND group_folder != ?
        ) AS has_other_owner
        """,
        (
            target_session_id,
            plan.conversation_id,
            target_session_id,
            target_session_id,
            plan.routed_folder,
        ),
    )
    row = await cursor.fetchone()
    return row is not None and bool(row["has_other_owner"])


async def _repair_sources_are_available(
    database: Connection,
    plan: _RuntimeOwnershipRepair,
) -> bool:
    for source_folder in plan.source_folders:
        owner_cursor = await database.execute(
            "SELECT jid FROM registered_groups WHERE folder = ?",
            (source_folder,),
        )
        owner = await owner_cursor.fetchone()
        if owner is None or owner["jid"] == plan.thread_jid:
            continue
        message = (
            f"Routed runtime source {source_folder} is owned by "
            f"{owner['jid']}, not {plan.thread_jid}"
        )
        if plan.repairs_owner:
            raise RuntimeOwnershipRepairConflictError(message)
        logger.warning("Legacy routed runtime source migration blocked", reason=message)
        return False
    return True


async def _apply_runtime_ownership_repair(
    database: Connection,
    plan: _RuntimeOwnershipRepair,
) -> int:
    """Apply one preflighted repair across its separate state projections."""
    migrated = await _repair_conversation_owner(database, plan)
    migrated += await _repair_workspace_profile(database, plan)
    migrated += await _repair_in_flight_turns(database, plan)
    migrated += await _repair_session(database, plan)
    return migrated


async def _repair_conversation_owner(
    database: Connection,
    plan: _RuntimeOwnershipRepair,
) -> int:
    if not plan.repairs_owner:
        return 0
    repaired = await database.execute(
        "UPDATE routed_conversations SET workspace = ? WHERE id = ? AND workspace = ?",
        (
            plan.target_workspace,
            plan.conversation_id,
            plan.original_workspace,
        ),
    )
    return repaired.rowcount


async def _repair_workspace_profile(
    database: Connection,
    plan: _RuntimeOwnershipRepair,
) -> int:
    profile_source = plan.profile_source
    if profile_source is None or profile_source == plan.routed_folder:
        return 0
    moved = await database.execute(
        """
        UPDATE registered_groups
        SET folder = ?
        WHERE jid = ? AND folder = ?
        """,
        (plan.routed_folder, plan.thread_jid, profile_source),
    )
    return moved.rowcount


async def _repair_in_flight_turns(
    database: Connection,
    plan: _RuntimeOwnershipRepair,
) -> int:
    migrated = 0
    for source_folder in plan.source_folders:
        moved = await database.execute(
            """
            UPDATE in_flight_turns
            SET group_folder = ?
            WHERE chat_jid = ? AND group_folder = ?
            """,
            (plan.routed_folder, plan.thread_jid, source_folder),
        )
        migrated += moved.rowcount
    return migrated


async def _repair_session(
    database: Connection,
    plan: _RuntimeOwnershipRepair,
) -> int:
    if plan.session_id is None:
        return 0
    sessions_cursor = await database.execute(
        "SELECT group_folder FROM sessions WHERE session_id = ?",
        (plan.session_id,),
    )
    session_folders = {
        GroupFolder(str(session["group_folder"])) for session in await sessions_cursor.fetchall()
    }
    session_sources = [folder for folder in plan.source_folders if folder in session_folders]
    if not session_sources:
        return 0

    await database.execute(
        """
        INSERT INTO sessions (group_folder, session_id)
        VALUES (?, ?)
        ON CONFLICT(group_folder) DO UPDATE SET
            session_id = excluded.session_id
        """,
        (plan.routed_folder, plan.session_id),
    )
    for session_source in session_sources:
        await _merge_session_taint(database, session_source, plan.routed_folder)
        await database.execute(
            "DELETE FROM sessions WHERE group_folder = ?",
            (session_source,),
        )
        await database.execute(
            "DELETE FROM session_security_taint WHERE group_folder = ?",
            (session_source,),
        )
    return len(session_sources)


async def _merge_session_taint(
    database: Connection,
    source_folder: GroupFolder,
    target_folder: GroupFolder,
) -> None:
    await database.execute(
        """
        INSERT INTO session_security_taint (
            group_folder, corruption_tainted, secret_tainted, updated_at
        )
        SELECT ?, corruption_tainted, secret_tainted, updated_at
        FROM session_security_taint
        WHERE group_folder = ?
        ON CONFLICT(group_folder) DO UPDATE SET
            corruption_tainted = MAX(
                session_security_taint.corruption_tainted,
                excluded.corruption_tainted
            ),
            secret_tainted = MAX(
                session_security_taint.secret_tainted,
                excluded.secret_tainted
            ),
            updated_at = MAX(
                session_security_taint.updated_at,
                excluded.updated_at
            )
        """,
        (target_folder, source_folder),
    )
