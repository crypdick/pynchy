"""Tests for the unified channel reconciler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun import freeze_time

from pynchy.config.api import OwnerConfig, WorkspaceConfig
from pynchy.host.orchestrator.messaging.reconciler import reconcile_all_channels, reset_cooldowns
from pynchy.plugins.api import (
    Channel,
    InboundFetchResult,
    NewMessage,
    OutboundEventType,
)
from pynchy.state import (
    get_channel_cursor,
    get_pending_outbound,
    record_outbound,
    record_outbound_deliveries,
    set_channel_cursor,
    store_chat_metadata,
)
from pynchy.state.outbound import OutboundDelivery, OutboundDeliveryOperation
from pynchy.workspace.api import WorkspaceProfile
from tests.conftest import init_test_database, make_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="group@g.us",
    name="Test",
    folder="test",
    trigger="@pynchy",
    added_at="2024-01-01",
)


def _make_channel(
    *,
    name: str = "slack",
    connected: bool = True,
    owns: bool = True,
    inbound: list[NewMessage] | None = None,
    high_water_mark: str = "",
) -> MagicMock:
    ch = MagicMock(spec=Channel)
    ch.name = name
    ch.is_connected.return_value = connected
    ch.owns_jid = MagicMock(return_value=owns)
    ch.send_event = AsyncMock()
    msgs = inbound or []
    # Default high_water_mark to the latest message timestamp if not provided
    hwm = high_water_mark or (msgs[-1].timestamp if msgs else "")
    ch.fetch_inbound_since = AsyncMock(
        return_value=InboundFetchResult(messages=msgs, high_water_mark=hwm)
    )
    return ch


def _make_deps(
    channels: list | None = None,
    workspaces: dict | None = None,
) -> MagicMock:
    deps = MagicMock()
    deps.channels = channels or []
    deps.workspaces = workspaces or {}
    deps.queue = MagicMock()
    deps.ingest_user_message = AsyncMock()
    deps.start_interactive_turn = AsyncMock()
    return deps


@pytest.fixture
async def _db():
    await init_test_database()
    # Seed chat rows for the FK constraint
    for jid in ("group@g.us", "admin@g.us"):
        await store_chat_metadata(jid, "2024-01-01T00:00:00")


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    """Clear cooldowns before each test so reconciliation always runs."""
    reset_cooldowns()
    yield
    reset_cooldowns()


@pytest.fixture(autouse=True)
def _permissive_sender_defaults(monkeypatch):
    """Default to current composable workspace settings."""
    monkeypatch.setattr("pynchy.config.settings._settings", make_settings())


# ---------------------------------------------------------------------------
# Inbound reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestInboundReconciliation:
    @pytest.mark.asyncio
    async def test_ingests_new_messages(self):
        msg = NewMessage(
            id="msg-1",
            chat_jid="slack:C123",
            sender="U1",
            sender_name="Alice",
            content="hello",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")

        await reconcile_all_channels(deps)

        deps.ingest_user_message.assert_awaited_once()
        ingested_msg = deps.ingest_user_message.call_args[0][0]
        assert ingested_msg.chat_jid == "group@g.us"  # remapped to canonical
        deps.start_interactive_turn.assert_awaited_once_with("group@g.us")
        deps.queue.enqueue_message_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_advances_inbound_cursor(self):
        msg = NewMessage(
            id="msg-1",
            chat_jid="slack:C123",
            sender="U1",
            sender_name="Alice",
            content="hello",
            timestamp="2024-06-01T12:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")

        await reconcile_all_channels(deps)

        cursor = await get_channel_cursor("slack", "group@g.us", "inbound")
        assert cursor == "2024-06-01T12:00:00"

    @pytest.mark.asyncio
    async def test_successful_empty_initial_scan_persists_poll_start(self):
        poll_started_at = datetime(2024, 6, 1, 12, tzinfo=UTC)
        ch = _make_channel()
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with freeze_time(poll_started_at) as clock:
            await reconcile_all_channels(deps)
            assert (
                await get_channel_cursor("slack", "group@g.us", "inbound")
                == poll_started_at.isoformat()
            )

            clock.tick(delta=timedelta(hours=1))
            reset_cooldowns()
            await reconcile_all_channels(deps)

        fetches = ch.fetch_inbound_since.await_args_list
        assert fetches[0].args[1] == (poll_started_at - timedelta(hours=24)).isoformat()
        assert fetches[1].args[1] == poll_started_at.isoformat()

    @pytest.mark.asyncio
    async def test_failed_initial_scan_keeps_retrying_without_a_cursor(self):
        poll_started_at = datetime(2024, 6, 1, 12, tzinfo=UTC)
        ch = _make_channel()
        ch.fetch_inbound_since.side_effect = OSError("provider unavailable")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with freeze_time(poll_started_at) as clock:
            with pytest.raises(RuntimeError, match="OSError: provider unavailable"):
                await reconcile_all_channels(deps)
            clock.tick(delta=timedelta(hours=1))
            with pytest.raises(RuntimeError, match="OSError: provider unavailable"):
                await reconcile_all_channels(deps)

        assert not await get_channel_cursor("slack", "group@g.us", "inbound")
        fetches = ch.fetch_inbound_since.await_args_list
        assert fetches[0].args[1] == (poll_started_at - timedelta(hours=24)).isoformat()
        assert fetches[1].args[1] == (poll_started_at - timedelta(hours=23)).isoformat()

    @pytest.mark.asyncio
    async def test_message_arriving_during_empty_fetch_is_recovered_next_pass(self):
        poll_started_at = datetime(2024, 6, 1, 12, tzinfo=UTC)
        arrival = poll_started_at + timedelta(minutes=2)
        msg = NewMessage(
            id="arrived-during-fetch",
            chat_jid="slack:C123",
            sender="U1",
            sender_name="Alice",
            content="do not skip me",
            timestamp=arrival.isoformat(),
        )
        ch = _make_channel()
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        fetch_count = 0

        with freeze_time(poll_started_at) as clock:

            def fetch(_jid: str, cursor: str) -> InboundFetchResult:
                nonlocal fetch_count
                fetch_count += 1
                if fetch_count == 1:
                    clock.tick(delta=timedelta(minutes=5))
                    return InboundFetchResult(messages=[])
                messages = [msg] if cursor <= msg.timestamp else []
                return InboundFetchResult(
                    messages=messages,
                    high_water_mark=msg.timestamp if messages else "",
                )

            ch.fetch_inbound_since.side_effect = fetch
            await reconcile_all_channels(deps)
            reset_cooldowns()
            await reconcile_all_channels(deps)

        deps.ingest_user_message.assert_awaited_once()
        assert await get_channel_cursor("slack", "group@g.us", "inbound") == arrival.isoformat()

    @pytest.mark.asyncio
    async def test_workspace_created_during_ingress_waits_for_next_cycle(self):
        msg = NewMessage(
            id="msg-creates-thread",
            chat_jid="slack:C123",
            sender="U1",
            sender_name="Alice",
            content="start a thread",
            timestamp="2024-06-01T00:00:00",
        )
        slack = _make_channel(name="slack", inbound=[msg])
        discord = _make_channel(name="discord")
        workspaces = {"group@g.us": TEST_GROUP}
        deps = _make_deps(channels=[slack, discord], workspaces=workspaces)

        def register_thread(*_args, **_kwargs):
            workspaces["discord:thread-1"] = WorkspaceProfile(
                jid="discord:thread-1",
                name="New thread",
                folder="test/threads/new-thread",
                trigger="@pynchy",
                added_at="2024-06-01",
            )

        deps.ingest_user_message.side_effect = register_thread
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")

        await reconcile_all_channels(deps)

        assert "discord:thread-1" in workspaces
        for channel in (slack, discord):
            reconciled_jids = [
                awaited.args[0] for awaited in channel.fetch_inbound_since.await_args_list
            ]
            assert reconciled_jids == ["group@g.us"]

    @pytest.mark.asyncio
    async def test_skips_channel_without_jid_or_ownership(self):
        ch = _make_channel(owns=False)
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)

        ch.fetch_inbound_since.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_aggregates_failed_pairs_after_healthy_pairs_finish(self):
        failed = _make_channel(name="slack")
        failed.fetch_inbound_since.side_effect = OSError("history unavailable")
        also_failed = _make_channel(name="discord")
        also_failed.fetch_inbound_since.side_effect = TimeoutError("history timed out")
        healthy = _make_channel(
            name="matrix",
            high_water_mark="2024-06-01T12:00:00",
        )
        deps = _make_deps(
            channels=[failed, also_failed, healthy],
            workspaces={"group@g.us": TEST_GROUP},
        )
        for channel_name in ("slack", "discord", "matrix"):
            await set_channel_cursor(
                channel_name,
                "group@g.us",
                "inbound",
                "2024-01-01T00:00:00",
            )

        with pytest.raises(RuntimeError) as raised:
            await reconcile_all_channels(deps)

        error = str(raised.value)
        assert "failed for 2 pair(s)" in error
        assert "slack/group@g.us: OSError: history unavailable" in error
        assert "discord/group@g.us: TimeoutError: history timed out" in error
        failed.fetch_inbound_since.assert_awaited_once()
        also_failed.fetch_inbound_since.assert_awaited_once()
        healthy.fetch_inbound_since.assert_awaited_once()
        assert await get_channel_cursor("matrix", "group@g.us", "inbound") == "2024-06-01T12:00:00"


# ---------------------------------------------------------------------------
# Outbound retry
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
@pytest.mark.action("message.outbound.retry")
class TestOutboundRetry:
    @pytest.mark.asyncio
    async def test_retries_pending_outbound(self):
        # Record a pending outbound message
        await record_outbound("group@g.us", "retry me", "broadcast", ["slack"])

        ch = _make_channel()
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)

        ch.send_event.assert_awaited_once()
        target_jid, event = ch.send_event.call_args[0]
        assert target_jid == "group@g.us"
        assert event.type is OutboundEventType.TEXT
        assert event.content == "retry me"

        # Should be marked as delivered
        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_retries_pending_edit_without_posting_a_duplicate(self):
        await record_outbound_deliveries(
            "group@g.us",
            "accumulated trace",
            "agent_trace",
            [
                OutboundDelivery(
                    channel_name="slack",
                    operation=OutboundDeliveryOperation.EDIT,
                    remote_message_id="message-123",
                )
            ],
        )
        ch = _make_channel()
        ch.update_event = AsyncMock()
        deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})

        await reconcile_all_channels(deps)

        ch.update_event.assert_awaited_once()
        assert ch.update_event.await_args.args[:2] == ("group@g.us", "message-123")
        ch.send_event.assert_not_awaited()
        assert not await get_pending_outbound("slack", "group@g.us")

    @pytest.mark.asyncio
    async def test_failed_edit_retries_as_a_truthful_fallback_post(self):
        await record_outbound_deliveries(
            "group@g.us",
            "accumulated trace",
            "agent_trace",
            [
                OutboundDelivery(
                    channel_name="slack",
                    operation=OutboundDeliveryOperation.EDIT,
                    remote_message_id="message-123",
                )
            ],
        )
        ch = _make_channel()
        ch.update_event = AsyncMock(side_effect=OSError("message unavailable"))
        deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})

        await reconcile_all_channels(deps)

        ch.update_event.assert_awaited_once()
        ch.send_event.assert_awaited_once()
        assert ch.send_event.await_args.args[1].content == "accumulated trace"
        assert not await get_pending_outbound("slack", "group@g.us")

    @pytest.mark.asyncio
    async def test_records_error_on_retry_failure(self):
        await record_outbound("group@g.us", "fail me", "broadcast", ["slack"])

        ch = _make_channel()
        ch.send_event.side_effect = OSError("network down")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with pytest.raises(
            RuntimeError,
            match=r"slack/group@g\.us: outbound retry: OSError: network down",
        ):
            await reconcile_all_channels(deps)

        # Still pending (error recorded, delivered_at still NULL)
        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_failed_pair_can_recover_without_waiting_for_cooldown(self):
        await record_outbound("group@g.us", "retry me", "broadcast", ["slack"])

        ch = _make_channel()
        ch.send_event.side_effect = [OSError("network down"), None]
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with pytest.raises(RuntimeError, match="network down"):
            await reconcile_all_channels(deps)
        await reconcile_all_channels(deps)

        assert ch.send_event.await_count == 2
        assert not await get_pending_outbound("slack", "group@g.us")

    @pytest.mark.asyncio
    async def test_preserves_ordering_on_failure(self):
        """When a delivery fails, later messages are not sent (ordering preserved)."""
        await record_outbound("group@g.us", "first", "broadcast", ["slack"])
        await record_outbound("group@g.us", "second", "broadcast", ["slack"])

        ch = _make_channel()
        ch.send_event.side_effect = OSError("network down")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with pytest.raises(
            RuntimeError,
            match=r"slack/group@g\.us: outbound retry: OSError: network down",
        ):
            await reconcile_all_channels(deps)

        # Only one send attempted (breaks after first failure)
        assert ch.send_event.await_count == 1
        # Both still pending
        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 2


# ---------------------------------------------------------------------------
# Cooldown behaviour
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestCooldown:
    @pytest.mark.asyncio
    async def test_second_call_within_cooldown_is_skipped(self):
        ch = _make_channel()
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)
        first_count = ch.fetch_inbound_since.await_count

        # Second call — should be skipped due to cooldown
        await reconcile_all_channels(deps)
        assert ch.fetch_inbound_since.await_count == first_count

    @pytest.mark.asyncio
    async def test_runs_after_cooldown_reset(self):
        ch = _make_channel()
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)
        reset_cooldowns()
        await reconcile_all_channels(deps)

        assert ch.fetch_inbound_since.await_count == 2


# ---------------------------------------------------------------------------
# Cursor GC
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestCursorGC:
    @pytest.mark.asyncio
    async def test_owned_pair_failure_preserves_its_recovery_cursor(self):
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-06-01")
        slack = _make_channel(name="slack")
        slack.fetch_inbound_since.side_effect = OSError("history unavailable")
        deps = _make_deps(
            channels=[slack],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with pytest.raises(RuntimeError, match="OSError: history unavailable"):
            await reconcile_all_channels(deps)

        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-06-01"

    @pytest.mark.asyncio
    async def test_retains_exactly_owned_active_channel_workspace_pairs(self):
        await set_channel_cursor("dead-channel", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-06-01")
        await set_channel_cursor("slack", "removed@g.us", "inbound", "2024-05-01")
        await set_channel_cursor("discord", "group@g.us", "inbound", "2024-04-01")
        await set_channel_cursor("discord", "admin@g.us", "inbound", "2024-03-01")

        slack = _make_channel(name="slack")
        slack.owns_jid.side_effect = lambda jid: jid == "group@g.us"
        discord = _make_channel(name="discord")
        discord.owns_jid.side_effect = lambda jid: jid == "admin@g.us"
        deps = _make_deps(
            channels=[slack, discord],
            workspaces={
                "group@g.us": TEST_GROUP,
                "admin@g.us": WorkspaceProfile(
                    jid="admin@g.us",
                    name="Admin",
                    folder="admin",
                    trigger="@pynchy",
                    added_at="2024-01-01",
                ),
            },
        )

        await reconcile_all_channels(deps)

        assert not await get_channel_cursor("dead-channel", "group@g.us", "inbound")
        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-06-01"
        assert not await get_channel_cursor("slack", "removed@g.us", "inbound")
        assert not await get_channel_cursor("discord", "group@g.us", "inbound")
        assert await get_channel_cursor("discord", "admin@g.us", "inbound") == "2024-03-01"
        slack.fetch_inbound_since.assert_awaited_once_with("group@g.us", "2024-06-01")
        discord.fetch_inbound_since.assert_awaited_once_with("admin@g.us", "2024-03-01")


# ---------------------------------------------------------------------------
# Sender filter — reconciler must match _route_incoming_group behavior
# ---------------------------------------------------------------------------


ADMIN_GROUP = WorkspaceProfile(
    jid="admin@g.us",
    name="Admin",
    folder="admin",
    trigger="@pynchy",
    added_at="2024-01-01",
    is_admin=True,
)


def _owner_settings(*, workspace_folder: str = "test", owner: OwnerConfig | None = None):
    """Settings with owner identity and a current-schema workspace."""
    return make_settings(
        owner=owner or OwnerConfig(slack="U04OWNER"),
        workspaces={workspace_folder: WorkspaceConfig()},
    )


@pytest.mark.usefixtures("_db")
class TestSenderFilter:
    """Reconciler sender policy follows the current profile-composition bridge."""

    @pytest.mark.asyncio
    async def test_sender_filter_is_permissive_during_schema_cutover(self, monkeypatch):
        """Recovered messages are ingested without deleted allowed_users fields."""
        msg = NewMessage(
            id="msg-intruder",
            chat_jid="slack:C123",
            sender="U04INTRUDER",
            sender_name="Intruder",
            content="hack the planet",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")
        monkeypatch.setattr("pynchy.config.settings._settings", _owner_settings())

        await reconcile_all_channels(deps)

        deps.ingest_user_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_allowed_sender_ingested(self, monkeypatch):
        """Recovered messages from allowed senders ARE ingested."""
        msg = NewMessage(
            id="msg-owner",
            chat_jid="slack:C123",
            sender="U04OWNER",
            sender_name="Owner",
            content="hello",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")
        monkeypatch.setattr("pynchy.config.settings._settings", _owner_settings())

        await reconcile_all_channels(deps)

        deps.ingest_user_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_allowed_sender_name_ingested(self, monkeypatch):
        """Owner can be configured by Slack display name while messages keep raw IDs."""
        msg = NewMessage(
            id="msg-owner-name",
            chat_jid="slack:C123",
            sender="U04OWNER",
            sender_name="Alice",
            content="hello",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")
        monkeypatch.setattr(
            "pynchy.config.settings._settings",
            _owner_settings(owner=OwnerConfig(slack="alice")),
        )

        await reconcile_all_channels(deps)

        deps.ingest_user_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_admin_group_bypasses_sender_filter(self, monkeypatch):
        """Admin groups accept all senders — no filtering applied."""
        msg = NewMessage(
            id="msg-random",
            chat_jid="slack:C123",
            sender="U04RANDOM",
            sender_name="Random",
            content="admin stuff",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"admin@g.us": ADMIN_GROUP},
        )
        await set_channel_cursor("slack", "admin@g.us", "inbound", "2024-01-01T00:00:00")
        # Even with restrictive owner-only settings, admin groups pass everything
        monkeypatch.setattr(
            "pynchy.config.settings._settings",
            _owner_settings(workspace_folder="admin"),
        )

        await reconcile_all_channels(deps)

        deps.ingest_user_message.assert_awaited_once()
