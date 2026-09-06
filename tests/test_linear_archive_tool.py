"""Native Linear archiving through the public MCP and provider boundaries."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pynchy.plugins.integrations.api import LinearClient
from tests.linear_integration_support import start_mcp_client


@pytest.mark.action("linear.issue.archive")
@pytest.mark.parametrize(
    ("receipt", "error"),
    [
        (
            {"success": True, "entity": {"id": "issue-1", "archivedAt": "2026-09-06T20:00:00Z"}},
            None,
        ),
        ({"success": False}, "Linear did not archive the issue"),
        ({"success": True}, "Linear archive response did not confirm the requested issue"),
        (
            {"success": True, "entity": {"id": "other", "archivedAt": "2026-09-06T20:00:00Z"}},
            "Linear archive response did not confirm the requested issue",
        ),
        (
            {"success": True, "entity": {"id": "issue-1", "archivedAt": None}},
            "Linear archive response did not confirm the requested issue",
        ),
    ],
)
async def test_archive_requires_exact_provider_receipt(monkeypatch, receipt, error):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    requests = []

    async def graphql(request):
        requests.append(await request.json())
        return web.json_response({"data": {"issueArchive": receipt}})

    app = web.Application()
    app.router.add_post("/graphql", graphql)
    provider = TestServer(app)
    await provider.start_server()

    def provider_client(**kwargs):
        return LinearClient(endpoint=str(provider.make_url("/graphql")), **kwargs)

    try:
        with patch("pynchy.plugins.integrations.linear.LinearClient", side_effect=provider_client):
            client = await start_mcp_client()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_archive_issue",
                            "arguments": {"issue_id": "issue-1"},
                        },
                    },
                )
                result = (await response.json())["result"]
            finally:
                await client.close()
    finally:
        await provider.close()

    assert len(requests) == 1
    assert requests[0]["variables"] == {"issue_id": "issue-1"}
    assert "issueArchive(id: $issue_id, trash: false)" in requests[0]["query"]
    if error:
        assert result["isError"] is True
        assert error in result["content"][0]["text"]
    else:
        assert not result.get("isError")
        assert json.loads(result["content"][0]["text"]) == receipt["entity"]


@pytest.mark.parametrize(
    "arguments", [{}, {"issue_id": ""}, {"issue_id": "   "}, {"issue_id": "issue-1", "trash": True}]
)
async def test_archive_rejects_invalid_or_destructive_arguments(monkeypatch, arguments):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    with patch("pynchy.plugins.integrations.linear.LinearClient") as provider:
        client = await start_mcp_client()
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "linear_archive_issue", "arguments": arguments},
                },
            )
            assert (await response.json())["result"]["isError"] is True
        finally:
            await client.close()
        provider.assert_not_called()
