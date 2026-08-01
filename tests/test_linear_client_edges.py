"""Public Linear client behavior for malformed and unsuccessful responses."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.plugins.integrations.linear import LinearClient, LinearError


class _PostContext:
    def __init__(self, response: MagicMock) -> None:
        self._response = response

    async def __aenter__(self) -> MagicMock:
        return self._response

    async def __aexit__(self, *_args: object) -> None:
        return None


def _query_client(body: object, *, status: int = 200) -> LinearClient:
    response = MagicMock(status=status)
    response.json = AsyncMock(return_value=body)
    session = MagicMock()
    session.post.return_value = _PostContext(response)
    return LinearClient(api_key="lin_api_test", session=session)  # pragma: allowlist secret


class TestLinearClientQueryEdges:
    async def test_query_converts_malformed_response_to_linear_error(self):
        response = MagicMock(status=502)
        response.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "body", 0))
        session = MagicMock()
        session.post.return_value = _PostContext(response)
        client = LinearClient(api_key="lin_api_test", session=session)

        with pytest.raises(LinearError, match="HTTP 502"):
            await client.query("query Viewer { viewer { id } }")

    async def test_query_rejects_non_success_http_status(self):
        client = _query_client({"data": {}}, status=503)

        with pytest.raises(LinearError, match="HTTP 503"):
            await client.query("query Viewer { viewer { id } }")

    async def test_query_requires_a_data_object(self):
        client = _query_client({"data": []})

        with pytest.raises(LinearError, match="data object"):
            await client.query("query Viewer { viewer { id } }")


class TestLinearClientResponseEdges:
    async def test_list_issues_without_team_filter_passes_first_to_query(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value={"issues": {"nodes": [{"id": "issue-1"}]}})

        assert await client.list_issues(first=7) == [{"id": "issue-1"}]
        assert client.query.await_args.kwargs == {"first": 7}

    @pytest.mark.parametrize(
        ("method", "response", "message"),
        [
            (
                "create_issue",
                {"issueCreate": {"success": False}},
                "did not create the issue",
            ),
            (
                "create_issue",
                {"issueCreate": {"success": True, "issue": None}},
                "include an issue",
            ),
        ],
    )
    async def test_create_issue_rejects_unsuccessful_or_missing_issue(
        self, method: str, response: dict[str, object], message: str
    ):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value=response)

        with pytest.raises(LinearError, match=message):
            await client.create_issue(team_id="team-1", title="Test issue")

    async def test_get_issue_rejects_non_object_issue(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value={"issue": ["not-an-issue"]})

        with pytest.raises(LinearError, match="not an object"):
            await client.get_issue("issue-1")

    async def test_get_issue_reraises_unrelated_provider_error(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(side_effect=LinearError("Linear request failed"))

        with pytest.raises(LinearError, match="Linear request failed"):
            await client.get_issue("issue-1")

    async def test_get_issue_returns_provider_object(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        issue = {"id": "issue-1", "identifier": "SYN-1"}
        client.query = AsyncMock(return_value={"issue": issue})

        assert await client.get_issue("issue-1") == issue

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            ({"commentCreate": {"success": False}}, "did not create the comment"),
            ({"commentCreate": {"success": True, "comment": None}}, "comment"),
        ],
    )
    async def test_create_comment_rejects_unsuccessful_or_missing_comment(
        self, response: dict[str, object], message: str
    ):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value=response)

        with pytest.raises(LinearError, match=message):
            await client.create_comment("issue-1", "A comment")

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            ({"attachmentCreate": {"success": False}}, "did not create the attachment"),
            ({"attachmentCreate": {"success": True, "attachment": None}}, "attachment"),
        ],
    )
    async def test_create_attachment_rejects_unsuccessful_or_missing_attachment(
        self, response: dict[str, object], message: str
    ):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value=response)

        with pytest.raises(LinearError, match=message):
            await client.create_attachment("issue-1", "https://example.com", "Example")

    async def test_delete_issue_requires_provider_success(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value={"issueDelete": {"success": False}})

        with pytest.raises(LinearError, match="did not delete the issue"):
            await client.delete_issue("issue-1")

    @pytest.mark.parametrize(
        ("method", "response", "message"),
        [
            ("list_teams", {}, "teams"),
            ("list_issues", {"issues": {"nodes": {}}}, "nodes"),
            ("find_issues_by_attachment_url", {}, "attachmentsForURL"),
        ],
    )
    async def test_list_methods_reject_malformed_connections(
        self, method: str, response: dict[str, object], message: str
    ):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value=response)

        if method == "list_teams":
            call = client.list_teams()
        elif method == "list_issues":
            call = client.list_issues()
        else:
            call = client.find_issues_by_attachment_url("https://example.com")
        with pytest.raises(LinearError, match=message):
            await call
