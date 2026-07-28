"""Typed host handlers for the reviewed Gog action surface."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from pynchy.content_fencing import fence_untrusted_content
from pynchy.plugins.integrations.gog._client import GogError, create_gog_client
from pynchy.plugins.integrations.gog._models import (
    DocumentArguments,
    DocumentExportArguments,
    DraftArguments,
    MailArguments,
    MessageArguments,
    OAuthRedirectArguments,
    SearchArguments,
    SheetArguments,
    SheetUpdateArguments,
    request_arguments,
)


def _fenced_result(result: str, *, source: str) -> dict[str, object]:
    return {"result": fence_untrusted_content(result, source=source)}


def _write_result(result: str) -> dict[str, object]:
    return {"result": result}


async def handle_gmail_search(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(SearchArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(
            client.gmail_search,
            query=arguments.query,
            limit=arguments.limit,
        )
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Gmail search: {exc}"}
    return _fenced_result(result, source="Google Workspace Gmail via Gog")


async def handle_gmail_get(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(MessageArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(client.gmail_get, message_id=arguments.message_id)
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Gmail read: {exc}"}
    return _fenced_result(result, source="Google Workspace Gmail via Gog")


async def handle_gmail_create_draft(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(MailArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(
            client.gmail_create_draft,
            to=arguments.to,
            cc=arguments.cc,
            bcc=arguments.bcc,
            subject=arguments.subject,
            body=arguments.body,
        )
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Gmail draft: {exc}"}
    return _write_result(result)


async def handle_gmail_send_draft(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(DraftArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(client.gmail_send_draft, draft_id=arguments.draft_id)
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Gmail draft send: {exc}"}
    return _write_result(result)


async def handle_gmail_send(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(MailArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(
            client.gmail_send,
            to=arguments.to,
            cc=arguments.cc,
            bcc=arguments.bcc,
            subject=arguments.subject,
            body=arguments.body,
        )
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Gmail send: {exc}"}
    return _write_result(result)


async def handle_contacts_search(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(SearchArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(
            client.contacts_search,
            query=arguments.query,
            limit=arguments.limit,
        )
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog contacts search: {exc}"}
    return _fenced_result(result, source="Google Workspace contacts via Gog")


async def handle_docs_read(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(DocumentArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(
            client.docs_read,
            document_id=arguments.document_id,
            tab=arguments.tab,
        )
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Docs read: {exc}"}
    return _fenced_result(result, source="Google Workspace Docs via Gog")


async def handle_docs_export(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(DocumentExportArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(
            client.docs_export,
            document_id=arguments.document_id,
            export_format=arguments.format,
        )
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Docs export: {exc}"}
    return _fenced_result(result, source="Google Workspace Docs via Gog")


async def handle_sheets_get(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(SheetArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(
            client.sheets_get,
            spreadsheet_id=arguments.spreadsheet_id,
            range_name=arguments.range,
        )
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Sheets read: {exc}"}
    return _fenced_result(result, source="Google Workspace Sheets via Gog")


async def handle_sheets_update(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(SheetUpdateArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(
            client.sheets_update,
            spreadsheet_id=arguments.spreadsheet_id,
            range_name=arguments.range,
            values=arguments.values,
            input_mode=arguments.input_mode,
        )
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Invalid Gog Sheets update: {exc}"}
    return _write_result(result)


async def handle_setup_start(_data: dict[str, Any]) -> dict[str, object]:
    try:
        result = await asyncio.to_thread(create_gog_client().setup_start)
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Gog setup could not start: {exc}"}
    return _write_result(result)


async def handle_setup_complete(data: dict[str, Any]) -> dict[str, object]:
    try:
        arguments = request_arguments(OAuthRedirectArguments, data)
        client = create_gog_client()
        result = await asyncio.to_thread(client.setup_complete, redirect_url=arguments.redirect_url)
    except (GogError, ValidationError, ValueError) as exc:
        return {"error": f"Gog setup could not complete: {exc}"}
    return _write_result(result)
