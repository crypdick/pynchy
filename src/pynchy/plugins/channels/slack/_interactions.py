"""Slack Block Kit interactive-callback handlers.

Handles the three interactive widgets pynchy renders into Slack messages:
ask_user submissions, cop approval buttons, and the agent Stop button.

A composed collaborator of :class:`SlackChannel` (not a mixin). The channel
constructs one of these and the events collaborator routes interaction
callbacks to it. It holds a back-reference to the channel to reach the live
``_app`` client, the allowlist collaborator, and the ``_on_*`` callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pynchy.logger import logger

from ._ids import jid as slack_jid
from ._ui import extract_checkbox_values, extract_text_input_value

if TYPE_CHECKING:
    from ._channel import SlackChannel
else:
    # beartype resolves the ``channel: SlackChannel`` forward ref at call time
    # from this module's globals. ``_channel`` imports this module, so a real
    # runtime import would be circular — bind a permissive substitute so the
    # forward ref resolves (mypy uses the real type from the branch above).
    SlackChannel = object


@dataclass(slots=True)
class SlackInteractions:
    """Interactive Block Kit callbacks for :class:`SlackChannel`."""

    _channel: SlackChannel

    async def on_ask_user_interaction(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        await self._on_ask_user_interaction(body, action)

    async def on_approval_interaction(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        await self._on_approval_interaction(body, action)

    async def on_agent_stop_interaction(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        await self._on_agent_stop_interaction(body, action)

    async def _finalize_decision(  # noqa: PLR0913 - shared callback helper keeps interaction handlers small.
        self,
        body: dict[str, Any],
        channel_id: str,
        message_ts: str,
        *,
        fallback: str,
        context_text: str,
        label: str,
    ) -> None:
        """Strip action buttons from the source message and append a context line.

        Shared by the approval and agent-stop handlers, which differ only in
        the confirmation wording.  Best-effort: a failed ``chat_update`` is
        logged at debug and swallowed.
        """
        if not (channel_id and message_ts):
            return
        original_blocks = body.get("message", {}).get("blocks", [])
        # Keep everything except the interactive buttons, then append confirmation.
        kept_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        kept_blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": context_text}]}
        )
        try:
            await self._channel.slack_app.client.chat_update(
                channel=channel_id, ts=message_ts, text=fallback, blocks=kept_blocks
            )
        except Exception as exc:  # noqa: BLE001 - interactive message updates are best-effort UX.
            logger.debug("failed to update message", label=label, err=str(exc))

    async def _on_ask_user_interaction(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        """Handle a block_actions interaction from an ask_user widget.

        Checkbox toggles fire as ``ask_user_checkbox_*`` but are ignored —
        we only act on the submit button (``ask_user_submit_*``), which
        reads the final checkbox selections and free-text value from
        ``state.values``.
        """
        ch = self._channel
        action_id = action.get("action_id", "")
        channel_id = body.get("channel", {}).get("id", "")

        # Guard: only process interactions from allowed channels.
        if channel_id and not ch.is_allowed_channel(channel_id):
            return

        # Ignore bare checkbox toggles — wait for submit.
        if action_id.startswith("ask_user_checkbox_"):
            return

        if not action_id.startswith("ask_user_submit_"):
            return

        message_ts = body.get("message", {}).get("ts", "")
        user_id = body.get("user", {}).get("id", "")

        request_id = action_id.removeprefix("ask_user_submit_")

        # Collect checkbox selections and free-text input.
        checkbox_answer = extract_checkbox_values(body, request_id)
        text_answer = extract_text_input_value(body, request_id)

        # Prefer text if provided, otherwise use checkbox selections.
        answer = text_answer if text_answer else checkbox_answer

        answer_dict = {
            "answer": answer,
            "answered_by": user_id,
            "channel_id": channel_id,
            "message_ts": message_ts,
        }

        if ch.on_ask_user_answer:
            ch.on_ask_user_answer(request_id, answer_dict)

        # Update the question message to show the answer and remove interactivity
        if channel_id and message_ts:
            answered_text = f"Answered: *{answer}*"
            try:
                await ch.slack_app.client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=answered_text,
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": answered_text},
                        }
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - ask_user message updates are best-effort UX.
                logger.debug("Failed to update ask_user message", err=str(exc))

    async def _on_approval_interaction(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        """Handle an approval button click (Approve or Deny).

        Extracts the action and short_id from the ``action_id`` (e.g.
        ``cop_approve_a1``), invokes the approval callback, and updates the
        prompt message to remove buttons and show the decision.
        """
        ch = self._channel
        action_id = action.get("action_id", "")
        channel_id = body.get("channel", {}).get("id", "")

        if channel_id and not ch.is_allowed_channel(channel_id):
            return

        # Parse action_id: cop_{approve|deny}_{short_id}
        parts = action_id.split("_", 2)  # ["cop", "approve", "a1"]
        if len(parts) < 3:
            return
        decision = parts[1]  # "approve" or "deny"
        short_id = parts[2]

        message_ts = body.get("message", {}).get("ts", "")
        user_id = body.get("user", {}).get("id", "")
        user_name = body.get("user", {}).get("username", user_id)

        # Invoke the approval decision callback
        if ch.on_approval_decision and channel_id:
            jid = slack_jid(channel_id)
            ch.on_approval_decision(jid, decision, short_id, user_id)

        verb = "Approved" if decision.startswith("approve") else "Denied"
        await self._finalize_decision(
            body,
            channel_id,
            message_ts,
            fallback=f"{verb} by {user_name}",
            context_text=f"✅ {verb} by <@{user_id}>",
            label="approval",
        )

        logger.info(
            "Approval button clicked",
            decision=decision,
            short_id=short_id,
            user=user_id,
        )

    async def _on_agent_stop_interaction(
        self, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        """Handle a Stop button click during agent streaming.

        Extracts the ``group_name`` from the ``action_id`` (e.g.
        ``agent_stop_ops``), invokes the stop callback, and updates the
        message to show who stopped the agent.
        """
        ch = self._channel
        action_id = action.get("action_id", "")
        channel_id = body.get("channel", {}).get("id", "")

        if channel_id and not ch.is_allowed_channel(channel_id):
            return

        # Parse action_id: agent_stop_{group_name}
        group_name = action_id.removeprefix("agent_stop_")
        if not group_name:
            return

        message_ts = body.get("message", {}).get("ts", "")
        user_id = body.get("user", {}).get("id", "")
        user_name = body.get("user", {}).get("username", user_id)

        # Signal cancellation to the agent execution loop
        if ch.on_agent_stop:
            ch.on_agent_stop(group_name, user_id)

        await self._finalize_decision(
            body,
            channel_id,
            message_ts,
            fallback=f"Stopped by {user_name}",
            context_text=f"⏹ Stopped by <@{user_id}>",
            label="stop",
        )

        logger.info(
            "Agent stop button clicked",
            group=group_name,
            user=user_id,
        )
