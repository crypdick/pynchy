"""Semantic action declarations for the host-only Gog integration."""

from __future__ import annotations

from pynchy._action_contract import ActionSpec, ActionSurface, ActionTransport
from pynchy._action_spec_helpers import build_action

_GOG_ACTIONS = (
    ("integration.gog.auth.start", "gog_setup_start", "Start Gog OAuth authorization."),
    ("integration.gog.auth.complete", "gog_setup_complete", "Complete Gog OAuth authorization."),
    ("mail.gog.message.search", "gog_gmail_search", "Search Gmail messages through Gog."),
    ("mail.gog.message.read", "gog_gmail_get", "Read one Gmail message through Gog."),
    ("mail.gog.draft.create", "gog_gmail_create_draft", "Create a Gmail draft through Gog."),
    ("mail.gog.draft.send", "gog_gmail_send_draft", "Send a Gmail draft through Gog."),
    ("mail.gog.message.send", "gog_gmail_send", "Send a Gmail message through Gog."),
    ("contacts.gog.contact.search", "gog_contacts_search", "Search Google Contacts through Gog."),
    ("docs.gog.document.read", "gog_docs_read", "Read a Google Doc through Gog."),
    ("docs.gog.document.export", "gog_docs_export", "Export a Google Doc through Gog."),
    ("sheets.gog.range.read", "gog_sheets_get", "Read a Google Sheets range through Gog."),
    ("sheets.gog.range.write", "gog_sheets_update", "Update a Google Sheets range through Gog."),
)

GOG_ACTION_SPECS: tuple[ActionSpec, ...] = tuple(
    build_action(
        action_id,
        "gog",
        summary,
        ActionSurface(ActionTransport.AGENT_TOOL, tool_name),
    )
    for action_id, tool_name, summary in _GOG_ACTIONS
)
