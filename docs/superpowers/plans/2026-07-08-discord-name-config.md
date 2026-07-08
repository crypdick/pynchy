# Discord Name Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Discord workspace config use human-friendly guild and channel names while Pynchy stores runtime Discord snowflake IDs in workspace state.

**Architecture:** Keep legacy ID-based refs working, but allow `connection.discord.<name>.chat.<guild-key>.channels.<channel-key>` where the keys are config aliases or Discord names. Discord startup reconciliation resolves those aliases against live guild/channel names, creates missing text channels, and registers the resulting concrete `discord:channel:<id>` JID.

**Tech Stack:** Python, Pydantic config models, discord.py, pytest, mkdocs.

---

### Task 1: Name-Based Discord Config And Provisioning

**Files:**
- Modify: `tests/test_discord_config.py`
- Modify: `tests/test_discord_events.py`
- Modify: `tests/test_discord_access.py`
- Modify: `tests/test_discord_channel.py`
- Modify: `tests/test_workspace_reconcile.py`
- Modify: `src/pynchy/config/models.py`
- Modify: `src/pynchy/config/discord_refs.py`
- Modify: `src/pynchy/plugins/channels/discord/_events.py`
- Modify: `src/pynchy/plugins/channels/discord/_access.py`
- Modify: `src/pynchy/plugins/channels/discord/_channel.py`
- Modify: `src/pynchy/host/orchestrator/workspace_config.py`
- Modify: `docs/usage/channels.md`

- [ ] **Step 1: Write failing tests**

Add tests proving settings accepts name refs, inbound access can match names, DiscordChannel resolves existing named channels, creates missing named channels, and workspace reconciliation permits Discord auto-provisioning outside the command-center connection.

- [ ] **Step 2: Verify tests fail**

Run:

```bash
uv run pytest tests/test_discord_config.py::test_settings_accept_discord_workspace_name_ref tests/test_discord_events.py::test_guild_context_carries_names tests/test_discord_access.py::test_name_configured_guild_channel_allows_message tests/test_discord_channel.py::test_create_group_creates_named_discord_channel tests/test_workspace_reconcile.py::TestReconcileWorkspaces::test_discord_workspace_can_auto_create_configured_channel -q
```

Expected: fail because name refs, context name fields, `DiscordChannel.create_group`, and Discord-specific auto-provisioning are not implemented.

- [ ] **Step 3: Implement minimal behavior**

Add optional `name` fields to Discord guild/channel config, allow non-ID guild/channel ref keys, carry guild/channel names in inbound context, match access config by ID or name, and implement Discord channel lookup/create with the configured name or alias.

- [ ] **Step 4: Update user docs**

Change the Discord setup docs to prefer names in config and explain that Pynchy stores concrete Discord IDs after startup reconciliation.

- [ ] **Step 5: Verify**

Run:

```bash
uv run pytest tests/test_discord_config.py tests/test_discord_events.py tests/test_discord_access.py tests/test_discord_channel.py tests/test_workspace_reconcile.py -q
uv run mkdocs build --strict
```
