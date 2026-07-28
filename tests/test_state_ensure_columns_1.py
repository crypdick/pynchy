"""Tests for the database layer."""

from __future__ import annotations

import aiosqlite
import pytest

from pynchy.state import (
    get_messaging_stats,
    mark_delivered,
    record_outbound,
    store_chat_metadata,
)
from pynchy.state.schema import create_schema
from tests.state_support import (
    _store,
    _store_message_row,
)

pytest_plugins = ("tests.state_support",)


@pytest.mark.anyio
class TestEnsureColumns:
    """Test that _ensure_columns adds missing columns to existing tables."""

    async def test_promotes_legacy_closed_binding_to_conversation_intent(self):
        """Existing archived controls remain terminal after the intent migration."""
        db = await aiosqlite.connect(":memory:")
        await create_schema(db)
        await db.executescript(
            """
            INSERT INTO routed_conversations (
                id, workspace, subject_namespace, subject_key, session_id,
                control_closed, created_at, updated_at
            ) VALUES (
                'legacy-conversation', 'project', 'linear:org:issue', 'issue-1', NULL,
                0, '2026-07-27T00:00:00Z', '2026-07-27T00:00:00Z'
            );
            INSERT INTO conversation_control_bindings (
                conversation_id, surface, parent_workspace, parent_jid, thread_jid,
                title, closed, updated_at
            ) VALUES (
                'legacy-conversation', 'discord', 'project', 'discord:channel:project',
                'discord:channel:issue-1', '[SYN-89] Legacy issue', 1,
                '2026-07-27T00:00:00Z'
            );
            """
        )
        await db.execute("ALTER TABLE routed_conversations DROP COLUMN control_closed")

        await create_schema(db)

        cursor = await db.execute(
            "SELECT control_closed FROM routed_conversations WHERE id = 'legacy-conversation'"
        )
        assert await cursor.fetchone() == (1,)
        await db.close()

    async def test_adds_requester_delivery_turn_without_losing_executions(self):
        """Existing execution owners survive the new delivery correlation column."""
        db = await aiosqlite.connect(":memory:")
        await create_schema(db)
        await db.execute(
            """
            INSERT INTO work_item_executions (
                id, workspace, linear_issue_id, linear_issue_identifier,
                linear_issue_url, turn_id, attempt, initiated_by,
                observed_state_id, observed_state_name, status, evidence_refs,
                requester_delivery_status, created_at, updated_at
            ) VALUES (
                'execution-1', 'pynchy', 'issue-1', 'SYN-1',
                'https://linear.app/example/issue/SYN-1', 'owner-turn', 1,
                'linear-webhook:test', 'state-in-progress', 'In Progress',
                'in_progress', '[]', 'not_requested',
                '2026-07-26T00:00:00Z', '2026-07-26T00:00:00Z'
            )
            """
        )
        await db.execute("ALTER TABLE work_item_executions DROP COLUMN requester_delivery_turn_id")

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(work_item_executions)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "requester_delivery_turn_id" in columns
        cursor = await db.execute(
            "SELECT turn_id, requester_delivery_turn_id "
            "FROM work_item_executions WHERE id = 'execution-1'"
        )
        assert await cursor.fetchone() == ("owner-turn", None)
        await db.close()

    async def test_migrates_stale_work_item_outcomes_without_losing_blocker_evidence(self):
        """Existing terminal projections clear only after preserving transition evidence."""
        db = await aiosqlite.connect(":memory:")
        await create_schema(db)
        await db.executescript(
            """
            INSERT INTO work_item_executions (
                id, workspace, linear_issue_id, linear_issue_identifier,
                linear_issue_url, attempt, initiated_by, observed_state_id,
                observed_state_name, status, summary, blocker, handoff_to,
                evidence_refs, requester_delivery_status, created_at, updated_at
            ) VALUES
                (
                    'completed-execution', 'pynchy', 'issue-completed', 'SYN-88',
                    'https://linear.app/example/issue/SYN-88', 1, 'linear-webhook:test',
                    'state-done', 'Done', 'completed', 'Publication succeeded.',
                    'GitHub permission missing', 'release operator', '[]', 'delivered',
                    '2026-07-26T00:00:00Z', '2026-07-26T00:10:00Z'
                ),
                (
                    'blocked-execution', 'pynchy', 'issue-blocked', 'SYN-99',
                    'https://linear.app/example/issue/SYN-99', 1, 'linear-webhook:test',
                    'state-blocked', 'Blocked', 'blocked', 'Deployment is blocked.',
                    'Deployment credential missing', 'release operator', '[]', 'delivered',
                    '2026-07-26T00:00:00Z', '2026-07-26T00:10:00Z'
                );
            INSERT INTO work_item_transitions (
                execution_id, request_id, operation, target_status,
                result_execution_status, evidence_refs, status, created_at, resolved_at
            ) VALUES
                (
                    'completed-execution', 'blocked-completed', 'move_to_blocked', 'blocked',
                    'blocked', '[]', 'succeeded',
                    '2026-07-26T00:01:00Z', '2026-07-26T00:01:01Z'
                ),
                (
                    'blocked-execution', 'blocked-current', 'move_to_blocked', 'blocked',
                    'blocked', '[]', 'succeeded',
                    '2026-07-26T00:01:00Z', '2026-07-26T00:01:01Z'
                );
            """
        )
        await db.execute("ALTER TABLE work_item_transitions DROP COLUMN summary")
        await db.execute("ALTER TABLE work_item_transitions DROP COLUMN blocker")
        await db.execute("ALTER TABLE work_item_transitions DROP COLUMN handoff_to")

        await create_schema(db)

        cursor = await db.execute(
            """
            SELECT status, blocker, handoff_to
            FROM work_item_executions
            ORDER BY id
            """
        )
        assert await cursor.fetchall() == [
            ("blocked", "Deployment credential missing", "release operator"),
            ("completed", None, None),
        ]
        cursor = await db.execute(
            """
            SELECT request_id, summary, blocker, handoff_to
            FROM work_item_transitions
            ORDER BY request_id
            """
        )
        assert await cursor.fetchall() == [
            (
                "blocked-completed",
                None,
                "GitHub permission missing",
                "release operator",
            ),
            (
                "blocked-current",
                None,
                "Deployment credential missing",
                "release operator",
            ),
        ]
        await db.close()

    async def test_adds_occurrence_state_to_existing_scheduled_tasks(self):
        """Existing one-shot tasks gain the initial generation without row loss."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                group_folder TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                next_run TEXT,
                last_run TEXT,
                last_result TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                session_policy TEXT NOT NULL DEFAULT 'reset_before_run',
                repo_access TEXT,
                input_source TEXT NOT NULL DEFAULT 'scheduled_task',
                config_job_name TEXT,
                derived_thread_name TEXT,
                bound_chat_jid TEXT,
                bound_group_folder TEXT,
                conversation_id TEXT,
                last_reset_occurrence TEXT
            );
            INSERT INTO scheduled_tasks (
                id, group_folder, chat_jid, prompt, schedule_type,
                schedule_value, status, created_at
            ) VALUES (
                'legacy-once', 'admin', 'slack:CADMIN', 'Continue work',
                'once', '2026-07-25T05:16:14+00:00', 'paused',
                '2026-07-25T04:45:00+00:00'
            );
        """)

        await create_schema(db)

        cursor = await db.execute(
            "SELECT occurrence_generation, occurrence_due_at, "
            "superseded_occurrence_generation, superseded_occurrence_due_at, memory_enabled "
            "FROM scheduled_tasks WHERE id = 'legacy-once'"
        )
        assert await cursor.fetchone() == (0, None, None, None, 1)
        await db.close()

    async def test_adds_missing_column_to_existing_table(self):
        """Simulate an old DB missing a column, then run create_schema."""
        db = await aiosqlite.connect(":memory:")
        # Create registered_groups WITHOUT is_admin column (old schema)
        await db.executescript("""
            CREATE TABLE registered_groups (
                jid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                folder TEXT NOT NULL UNIQUE,
                trigger_pattern TEXT NOT NULL,
                added_at TEXT NOT NULL,
                container_config TEXT
            );
        """)

        # Verify is_admin is missing
        cursor = await db.execute("PRAGMA table_info(registered_groups)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "is_admin" not in cols

        # create_schema is the public entry that runs the _ensure_columns
        # migration; on an old table it should add is_admin and security_profile.
        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(registered_groups)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "is_admin" in cols
        assert "security_profile" in cols

        await db.close()

    async def test_adds_active_control_state_to_existing_in_flight_turns(self):
        """Old checkpoints migrate to the active control state without row loss."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE in_flight_turns (
                turn_id TEXT PRIMARY KEY,
                chat_jid TEXT NOT NULL,
                group_folder TEXT NOT NULL,
                work_kind TEXT NOT NULL,
                input_messages TEXT NOT NULL,
                input_start_cursor TEXT NOT NULL,
                input_end_cursor TEXT NOT NULL,
                started_at TEXT NOT NULL,
                task_id TEXT,
                session_id TEXT,
                output_sent INTEGER NOT NULL DEFAULT 0,
                interrupted_at TEXT,
                deploy_id TEXT,
                claimed_at TEXT,
                scheduled_base_chat_jid TEXT,
                scheduled_thread_slot INTEGER,
                conversation_claim_id TEXT,
                input_source TEXT NOT NULL DEFAULT 'user'
            );
            INSERT INTO in_flight_turns (
                turn_id, chat_jid, group_folder, work_kind, input_messages,
                input_start_cursor, input_end_cursor, started_at,
                scheduled_base_chat_jid, scheduled_thread_slot
            ) VALUES (
                'legacy-turn', 'slack:C123', 'admin', 'interactive', '[]',
                '', 'cursor', '2026-07-25T10:00:00+00:00',
                'slack:legacy-parent', 7
            );
        """)

        await create_schema(db)

        cursor = await db.execute(
            "SELECT turn_id, control_state FROM in_flight_turns WHERE turn_id = 'legacy-turn'"
        )
        assert await cursor.fetchone() == ("legacy-turn", "active")
        cursor = await db.execute("PRAGMA table_info(in_flight_turns)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "scheduled_base_chat_jid" not in columns
        assert "scheduled_thread_slot" not in columns
        await db.close()

    async def test_adds_task_run_identity_columns_to_existing_ledger(self):
        """Startup migration preserves old rows while adding explicit run identity."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE task_run_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                run_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT,
                temporal_workflow_id TEXT,
                temporal_attempt INTEGER,
                error_signature TEXT,
                escalation_reason TEXT
            );
            INSERT INTO task_run_logs (
                task_id, run_at, duration_ms, status, temporal_workflow_id, temporal_attempt
            ) VALUES ('task-1', '2026-07-22T00:00:00Z', 10, 'success', 'workflow-1', 1);
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(task_run_logs)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert {"temporal_workflow_run_id", "turn_id"} <= cols
        cursor = await db.execute(
            "SELECT temporal_workflow_id, temporal_workflow_run_id, turn_id "
            "FROM task_run_logs WHERE task_id = 'task-1'"
        )
        assert await cursor.fetchone() == ("workflow-1", None, None)
        await db.close()

    async def test_adds_delivery_operation_to_existing_outbound_ledger(self):
        """Existing pending sends migrate to explicit post semantics."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE outbound_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_jid TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE outbound_deliveries (
                ledger_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                delivered_at TEXT,
                error TEXT,
                PRIMARY KEY (ledger_id, channel_name)
            );
            INSERT INTO outbound_ledger (
                chat_jid, content, timestamp, source
            ) VALUES (
                'discord:channel:1', 'pending', '2026-07-25T00:00:00Z', 'agent'
            );
            INSERT INTO outbound_deliveries (
                ledger_id, channel_name
            ) VALUES (1, 'discord');
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(outbound_deliveries)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert {"operation", "remote_message_id"} <= cols
        cursor = await db.execute(
            "SELECT operation, remote_message_id FROM outbound_deliveries WHERE ledger_id = 1"
        )
        assert await cursor.fetchone() == ("post", None)
        await db.close()

    async def test_noop_when_all_columns_present(self):
        """create_schema is idempotent when the schema is already up to date."""
        db = await aiosqlite.connect(":memory:")
        # First application builds the full schema; the second must not raise.
        await create_schema(db)
        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(registered_groups)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "is_admin" in cols
        await db.close()

    async def test_replaces_cached_task_thread_columns_with_config_job_provenance(self):
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                group_folder TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                next_run TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                persistent_thread_name TEXT,
                persistent_thread_jid TEXT
            );
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(scheduled_tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "config_job_name" in cols
        assert "config_job_command" in cols
        assert "persistent_thread_name" not in cols
        assert "persistent_thread_jid" not in cols
        await db.close()

    async def test_migrates_context_modes_once_then_drops_legacy_column(self):
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                group_folder TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                next_run TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                context_mode TEXT NOT NULL
            );
            INSERT INTO scheduled_tasks VALUES (
                'continued', 'group', 'group@g.us', 'continue', 'cron', '* * * * *',
                NULL, 'active', '2026-07-25T00:00:00Z', 'group'
            );
            INSERT INTO scheduled_tasks VALUES (
                'reset', 'group', 'group@g.us', 'reset', 'cron', '* * * * *',
                NULL, 'active', '2026-07-25T00:00:00Z', 'isolated'
            );
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(scheduled_tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "context_mode" not in cols
        cursor = await db.execute("SELECT id, session_policy FROM scheduled_tasks ORDER BY id")
        assert await cursor.fetchall() == [
            ("continued", "continue"),
            ("reset", "reset_before_run"),
        ]
        await create_schema(db)
        await db.close()

    async def test_renames_conversation_event_phoenix_ref(self):
        """create_schema migrates old projection refs to provider-neutral names."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE conversation_events (
                event_id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                kind TEXT NOT NULL,
                sender TEXT NOT NULL,
                sender_name TEXT,
                message_type TEXT NOT NULL,
                source_message_id TEXT,
                content_preview TEXT NOT NULL,
                phoenix_ref TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO conversation_events (
                event_id, turn_id, chat_jid, timestamp, kind, sender,
                message_type, content_preview, phoenix_ref
            ) VALUES (
                'evt_1', 'turn_1', 'slack:C123', '2026-07-10T00:00:00+00:00',
                'user_message', 'alice', 'user', 'hello', 'legacy:event:evt_1'
            );
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(conversation_events)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "trace_ref" in cols
        assert "phoenix_ref" not in cols

        cursor = await db.execute(
            "SELECT trace_ref FROM conversation_events WHERE event_id = 'evt_1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "legacy:event:evt_1"
        await db.close()


class TestMessagingStats:
    async def test_empty_db_returns_zeros(self):
        result = await get_messaging_stats()
        assert result["total_inbound"] == 0
        assert result["total_outbound"] == 0
        assert result["last_received_at"] is None
        assert result["last_sent_at"] is None
        assert result["pending_deliveries"] == 0

    async def test_counts_inbound_and_outbound(self):
        await store_chat_metadata("g@g.us", "2026-01-01T00:00:00", "Test")
        await _store_message_row(
            _store(
                message_id="m1",
                chat_jid="g@g.us",
                sender="u@s",
                sender_name="Alice",
                content="hello",
                timestamp="2026-02-20T10:00:00",
            )
        )
        await _store_message_row(
            _store(
                message_id="m2",
                chat_jid="g@g.us",
                sender="u@s",
                sender_name="Alice",
                content="world",
                timestamp="2026-02-20T10:00:01",
            )
        )

        await record_outbound("g@g.us", "hi back", "test", ["whatsapp"])

        result = await get_messaging_stats()
        assert result["total_inbound"] == 2
        assert result["total_outbound"] == 1
        assert result["last_received_at"] == "2026-02-20T10:00:01"
        assert result["last_sent_at"] is not None
        assert result["pending_deliveries"] == 1  # undelivered whatsapp entry

    async def test_pending_deliveries_excludes_delivered(self):
        await store_chat_metadata("g@g.us", "2026-01-01T00:00:00", "Test")

        ledger_id = await record_outbound("g@g.us", "msg", "test", ["whatsapp", "slack"])

        # Mark whatsapp as delivered, leave slack pending
        await mark_delivered(ledger_id, "whatsapp")

        result = await get_messaging_stats()
        assert result["total_outbound"] == 1
        assert result["pending_deliveries"] == 1  # only slack is pending
