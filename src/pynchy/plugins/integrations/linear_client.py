"""Typed GraphQL client shared by Linear MCP tools and operational canaries."""

from __future__ import annotations

import json
from typing import Any, cast

import aiohttp

LINEAR_API_URL = "https://api.linear.app/graphql"
_LINEAR_DATA_OBJECT_MISSING = "Linear response did not include a data object"
_LINEAR_ISSUE_NOT_CREATED = "Linear did not create the issue"
_LINEAR_ISSUE_CREATE_ISSUE_MISSING = "Linear issueCreate response did not include an issue"
_LINEAR_ISSUE_DELETE_FAILED = "Linear did not delete the issue"
_LINEAR_ISSUE_NOT_FOUND = "Entity not found: Issue"
_LINEAR_CONNECTION_MISSING = "Linear response did not include {key}"
_LINEAR_NODES_MISSING = "Linear response did not include {key}.nodes"


class LinearError(RuntimeError):
    """Raised when Linear returns GraphQL errors or an unexpected payload."""


class LinearClient:
    """Tiny async GraphQL client for Pynchy's Linear integration."""

    def __init__(
        self,
        *,
        api_key: str,
        session: object,
        endpoint: str = LINEAR_API_URL,
    ) -> None:
        self._api_key = api_key
        self._session = session
        self._endpoint = endpoint

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        payload = {"query": query, "variables": variables}
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }
        session = cast("Any", self._session)
        async with session.post(self._endpoint, json=payload, headers=headers) as response:
            try:
                body = await response.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                status = response.status if isinstance(response.status, int) else 500
                raise LinearError(f"Linear request failed with HTTP {status}") from exc

        if errors := body.get("errors"):
            messages = "; ".join(str(error.get("message", error)) for error in errors)
            raise LinearError(messages)
        status = response.status if isinstance(response.status, int) else 200
        if not 200 <= status < 300:
            # aiohttp's ClientResponseError retains request headers in its repr.
            # Converting HTTP failures here prevents an uncaught traceback from
            # printing the Linear authorization credential.
            raise LinearError(f"Linear request failed with HTTP {status}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearError(_LINEAR_DATA_OBJECT_MISSING)
        return data

    async def list_teams(self) -> list[dict[str, Any]]:
        data = await self.query(
            """
            query ListTeams {
              teams {
                nodes { id key name }
              }
            }
            """
        )
        return _nodes(data, "teams")

    async def list_issues(
        self,
        *,
        team_id: str | None = None,
        first: int = 50,
    ) -> list[dict[str, Any]]:
        if team_id:
            data = await self.query(
                """
                query ListTeamIssues($first: Int!, $teamId: String!) {
                  issues(first: $first, filter: { team: { id: { eq: $teamId } } }) {
                    nodes {
                      id identifier title url priority createdAt updatedAt
                      state { id name type }
                      team { id key name }
                      project { id name }
                    }
                  }
                }
                """,
                first=first,
                teamId=team_id,
            )
        else:
            data = await self.query(
                """
                query ListIssues($first: Int!) {
                  issues(first: $first) {
                    nodes {
                      id identifier title url priority createdAt updatedAt
                      state { id name type }
                      team { id key name }
                      project { id name }
                    }
                  }
                }
                """,
                first=first,
            )
        return _nodes(data, "issues")

    async def create_issue(  # noqa: PLR0913, RUF100 - Linear issue creation follows the API field set.
        self,
        *,
        team_id: str,
        title: str,
        description: str | None = None,
        project_id: str | None = None,
        state_id: str | None = None,
        label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        data = await self.query(
            """
            mutation CreateIssue(
              $team_id: String!,
              $title: String!,
              $description: String,
              $project_id: String,
              $state_id: String,
              $label_ids: [String!]
            ) {
              issueCreate(input: {
                teamId: $team_id,
                title: $title,
                description: $description,
                projectId: $project_id,
                stateId: $state_id,
                labelIds: $label_ids
              }) {
                success
                issue { id identifier title url }
              }
            }
            """,
            team_id=team_id,
            title=title,
            description=description,
            project_id=project_id,
            state_id=state_id,
            label_ids=label_ids,
        )
        result = data.get("issueCreate")
        if not isinstance(result, dict) or not result.get("success"):
            raise LinearError(_LINEAR_ISSUE_NOT_CREATED)
        issue = result.get("issue")
        if not isinstance(issue, dict):
            raise LinearError(_LINEAR_ISSUE_CREATE_ISSUE_MISSING)
        return issue

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        """Fetch one issue by ID for independent verification or cleanup."""
        try:
            data = await self.query(
                """
                query GetIssue($issue_id: String!) {
                  issue(id: $issue_id) {
                    id identifier title description url updatedAt
                    state { id name type }
                    project { id name }
                  }
                }
                """,
                issue_id=issue_id,
            )
        except LinearError as exc:
            # Linear reports a deleted issue as a GraphQL error, not a null field.
            if str(exc) == _LINEAR_ISSUE_NOT_FOUND:
                return None
            raise
        issue = data.get("issue")
        if issue is None:
            return None
        if not isinstance(issue, dict):
            raise LinearError("Linear issue response was not an object")
        return issue

    async def delete_issue(self, issue_id: str) -> None:
        """Permanently remove an issue created exclusively for a canary."""
        data = await self.query(
            """
            mutation DeleteIssue($issue_id: String!) {
              issueDelete(id: $issue_id, permanentlyDelete: true) { success }
            }
            """,
            issue_id=issue_id,
        )
        result = data.get("issueDelete")
        if not isinstance(result, dict) or not result.get("success"):
            raise LinearError(_LINEAR_ISSUE_DELETE_FAILED)


def _nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearError(_LINEAR_CONNECTION_MISSING.format(key=key))
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearError(_LINEAR_NODES_MISSING.format(key=key))
    return [node for node in nodes if isinstance(node, dict)]
