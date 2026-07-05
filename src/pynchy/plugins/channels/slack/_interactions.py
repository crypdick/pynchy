"""Slack Block Kit interactive-callback handlers.

Handles the three interactive widgets pynchy renders into Slack messages:
ask_user submissions, cop approval buttons, and the agent Stop button.

Split from ``_channel.py`` as a mixin so the channel module stays focused on
transport and lifecycle.  :class:`SlackChannel` mixes this in; every handler
uses the channel's own state (``self._app``, ``self._is_allowed_channel``,
and the ``self._on_*`` callbacks), so the split is behavior-preserving.
"""

from __future__ import annotations

from typing import Any

from pynchy.logger import logger

from ._ids import _jid
from ._ui import extract_checkbox_values, extract_text_input_value


class SlackInteractionMixin:
    """Interactive Block Kit callbacks for :class:`SlackChannel`."""

    async def _finalize_decision(
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
            await self._app.client.chat_update(
                channel=channel_id, ts=message_ts, text=fallback, blocks=kept_blocks
            )
        except Exception as exc:
            logger.debug(f"Failed to update {label} message", err=str(exc))

    async def _on_ask_user_interaction(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        """Handle a block_actions interaction from an ask_user widget.

        Checkbox toggles fire as ``ask_user_checkbox_*`` but are ignored —
        we only act on the submit button (``ask_user_submit_*``), which
        reads the final checkbox selections and free-text value from
        ``state.values``.
        """
        action_id = action.get("action_id", "")
        channel_id = body.get("channel", {}).get("id", "")

        # Guard: only process interactions from allowed channels.
        if channel_id and not self._is_allowed_channel(channel_id):
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

        if self._on_ask_user_answer:
            self._on_ask_user_answer(request_id, answer_dict)

        # Update the original message to show the answer and remove interactivity
        if channel_id and message_ts:
            answered_text = f"Answered: *{answer}*"
            try:
                await self._app.client.chat_update(
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
            except Exception as exc:
                logger.debug("Failed to update ask_user message", err=str(exc))

    async def _on_approval_interaction(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        """Handle an approval button click (Approve or Deny).

        Extracts the action and short_id from the ``action_id`` (e.g.
        ``cop_approve_a1``), invokes the approval callback, and updates the
        original message to remove buttons and show the decision.
        """
        action_id = action.get("action_id", "")
        channel_id = body.get("channel", {}).get("id", "")

        if channel_id and not self._is_allowed_channel(channel_id):
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
        if self._on_approval_decision and channel_id:
            jid = _jid(channel_id)
            self._on_approval_decision(jid, decision, short_id, user_id)

        verb = "Approved" if decision == "approve" else "Denied"
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
        action_id = action.get("action_id", "")
        channel_id = body.get("channel", {}).get("id", "")

        if channel_id and not self._is_allowed_channel(channel_id):
            return

        # Parse action_id: agent_stop_{group_name}
        group_name = action_id.removeprefix("agent_stop_")
        if not group_name:
            return

        message_ts = body.get("message", {}).get("ts", "")
        user_id = body.get("user", {}).get("id", "")
        user_name = body.get("user", {}).get("username", user_id)

        # Signal cancellation to the agent execution loop
        if self._on_agent_stop:
            self._on_agent_stop(group_name, user_id)

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
