"""One-time migration: promote channel aliases to canonical JIDs.

For each workspace whose canonical JID belongs to one channel but has an alias
on another (e.g. WhatsApp canonical with a Slack alias), this script re-keys
every DB row so the target-channel alias becomes the canonical JID.

Usage
-----
Stop the pynchy service, then run::

    uv run python scripts/migrate_to_single_channel.py --channel slack

After the script finishes, update config.toml::

    [channels]
    command_center = "slack"

Then restart the service.

Idempotent — safe to re-run.  Aliases whose alias_jid already matches the
canonical are skipped with a note.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


async def migrate(channel: str, db_path: Path) -> None:
    try:
        import aiosqlite
    except ImportError as exc:
        raise SystemExit("aiosqlite not available — run with 'uv run python ...'") from exc

    print(f"Opening {db_path}")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Collect aliases for the target channel
        cursor = await db.execute(
            "SELECT alias_jid, canonical_jid FROM jid_aliases WHERE channel_name = ?",
            (channel,),
        )
        rows = await cursor.fetchall()

        if not rows:
            print(f"No aliases found for channel '{channel}'. Nothing to migrate.")
            return

        migrated = 0
        skipped = 0

        for row in rows:
            target_jid: str = row["alias_jid"]
            old_canonical: str = row["canonical_jid"]

            if old_canonical == target_jid:
                print(f"  Skip (already canonical): {target_jid}")
                skipped += 1
                continue

            print(f"  Migrating: {old_canonical} → {target_jid}")

            async with db.execute("BEGIN"):
                pass  # aiosqlite handles transactions via context manager below

            await db.execute("BEGIN")
            try:
                # chats: jid is the PK
                await db.execute(
                    "UPDATE chats SET jid = ? WHERE jid = ?",
                    (target_jid, old_canonical),
                )
                # messages: chat_jid FK
                await db.execute(
                    "UPDATE messages SET chat_jid = ? WHERE chat_jid = ?",
                    (target_jid, old_canonical),
                )
                # outbound_ledger: chat_jid FK
                await db.execute(
                    "UPDATE outbound_ledger SET chat_jid = ? WHERE chat_jid = ?",
                    (target_jid, old_canonical),
                )
                # registered_groups: jid is PK
                await db.execute(
                    "UPDATE registered_groups SET jid = ? WHERE jid = ?",
                    (target_jid, old_canonical),
                )
                # channel_cursors: composite PK (channel_name, chat_jid, direction)
                await db.execute(
                    "UPDATE channel_cursors SET chat_jid = ? WHERE chat_jid = ?",
                    (target_jid, old_canonical),
                )
                # scheduled_tasks: chat_jid column
                await db.execute(
                    "UPDATE scheduled_tasks SET chat_jid = ? WHERE chat_jid = ?",
                    (target_jid, old_canonical),
                )

                # router_state: key = 'last_agent_timestamp', value = JSON {jid: ts}
                cursor2 = await db.execute(
                    "SELECT value FROM router_state WHERE key = 'last_agent_timestamp'"
                )
                row2 = await cursor2.fetchone()
                if row2 is not None:
                    data: dict[str, str] = json.loads(row2["value"])
                    if old_canonical in data:
                        data[target_jid] = data.pop(old_canonical)
                        await db.execute(
                            "UPDATE router_state SET value = ? WHERE key = 'last_agent_timestamp'",
                            (json.dumps(data),),
                        )

                # Remove the now-redundant alias row
                await db.execute(
                    "DELETE FROM jid_aliases WHERE alias_jid = ?",
                    (target_jid,),
                )

                await db.execute("COMMIT")
                migrated += 1
                print("    Done.")

            except Exception as exc:
                await db.execute("ROLLBACK")
                print(f"    ERROR — rolled back: {exc}")
                raise

    print(f"\nSummary: {migrated} migrated, {skipped} skipped.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        required=True,
        help="Target channel name (e.g. 'slack'). Aliases on this channel become canonical.",
    )
    parser.add_argument(
        "--db",
        default="data/messages.db",
        help="Path to messages.db (default: data/messages.db)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    asyncio.run(migrate(args.channel, db_path))


if __name__ == "__main__":
    main()
