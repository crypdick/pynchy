"""Typed GraphQL client shared by Linear MCP tools and operational canaries."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

import aiohttp

from pynchy.plugins.integrations.linear_statuses import TERMINAL_STATE_TYPES

LINEAR_API_URL = "https://api.linear.app/graphql"
_LINEAR_DATA_OBJECT_MISSING = "Linear response did not include a data object"
_LINEAR_ISSUE_NOT_CREATED = "Linear did not create the issue"
_LINEAR_ISSUE_CREATE_ISSUE_MISSING = "Linear issueCreate response did not include an issue"
_LINEAR_ISSUE_DELETE_FAILED = "Linear did not delete the issue"
_LINEAR_COMMENT_NOT_CREATED = "Linear did not create the comment"
_LINEAR_COMMENT_MISSING = "Linear commentCreate response did not include a comment"
_LINEAR_COMMENT_EVIDENCE_MISSING = "Linear commentCreate response lacks self-echo evidence"
_LINEAR_COMMENT_ISSUE_MISMATCH = "Linear commentCreate response belongs to another issue"
_LINEAR_ISSUE_STATE_EVIDENCE_MISSING = "Linear issueUpdate response lacks self-echo evidence"
_LINEAR_ISSUE_STATE_ISSUE_MISMATCH = "Linear issueUpdate response belongs to another issue"
_LINEAR_ISSUE_STATE_TARGET_MISMATCH = "Linear issueUpdate response has another state"
_LINEAR_ATTACHMENT_NOT_CREATED = "Linear did not create the attachment"
_LINEAR_ATTACHMENT_MISSING = "Linear attachmentCreate response did not include an attachment"
_LINEAR_ISSUE_NOT_FOUND = "Entity not found: Issue"
_LINEAR_CONNECTION_MISSING = "Linear response did not include {key}"
_LINEAR_NODES_MISSING = "Linear response did not include {key}.nodes"

CommentCreatedRecorder = Callable[[dict[str, Any]], Awaitable[None]]
IssueStateUpdatedRecorder = Callable[[dict[str, Any]], Awaitable[None]]


@runtime_checkable
class LinearQueryClient(Protocol):
    """Minimal Linear boundary shared by mutation helpers and query-only clients."""

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        """Run a Linear GraphQL operation."""


@runtime_checkable
class LinearIssueStateUpdateRecorder(Protocol):
    """Optional host boundary that persists state-update echo evidence."""

    async def record_issue_state_update(
        self,
        issue: dict[str, Any],
        *,
        issue_id: str,
        state_id: str,
    ) -> None:
        """Persist a callback marker for a confirmed host-owned state update."""


async def record_issue_state_update_if_supported(
    client: object,
    issue: dict[str, Any],
    *,
    issue_id: str,
    state_id: str,
) -> None:
    """Persist a state marker only when this client owns the host echo ledger."""
    if isinstance(client, LinearIssueStateUpdateRecorder):
        await client.record_issue_state_update(
            issue,
            issue_id=issue_id,
            state_id=state_id,
        )


@dataclass(frozen=True)
class LinearSelfEchoRecorder:
    """Host-owned callbacks that persist exact provider self-echo evidence."""

    comment_created: CommentCreatedRecorder | None = None
    issue_state_updated: IssueStateUpdatedRecorder | None = None


class _AuthorizationSecret(str):
    """Authorization credential whose diagnostic representation is redacted."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted Linear authorization>"


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
        team_key: str | None = None,
        self_echo_recorder: LinearSelfEchoRecorder | None = None,
    ) -> None:
        # aiohttp requires a string header value, while rich tracebacks
        # render frame locals with repr(). Keeping the semantic secret wrapper
        # in the headers prevents provider errors from printing the credential.
        self._api_key = _AuthorizationSecret(api_key)
        self._session = session
        self._endpoint = endpoint
        self._team_key = team_key
        self._self_echo_recorder = self_echo_recorder

    @property
    def team_key(self) -> str | None:
        """Return the team selector paired with this client's credential."""
        return self._team_key

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
        issue_input: dict[str, object] = {
            "teamId": team_id,
            "title": title,
        }
        optional_fields = {
            "description": description,
            "projectId": project_id,
            "stateId": state_id,
            "labelIds": label_ids,
        }
        # Linear distinguishes omitted optional create fields from explicit nulls
        # and rejects the latter with its generic "Argument Validation Error".
        issue_input.update(
            {key: value for key, value in optional_fields.items() if value is not None}
        )
        data = await self.query(
            """
            mutation CreateIssue($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success
                issue { id identifier title url }
              }
            }
            """,
            input=issue_input,
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

    async def create_comment(self, issue_id: str, body: str) -> dict[str, Any]:
        """Add an ordinary comment and retain its exact provider revision evidence."""
        data = await self.query(
            """
            mutation CreateComment($issue_id: String!, $body: String!) {
              commentCreate(input: { issueId: $issue_id, body: $body }) {
                success
                comment { id body createdAt updatedAt issue { id } }
              }
            }
            """,
            issue_id=issue_id,
            body=body,
        )
        result = data.get("commentCreate")
        if not isinstance(result, dict) or not result.get("success"):
            raise LinearError(_LINEAR_COMMENT_NOT_CREATED)
        comment = result.get("comment")
        if not isinstance(comment, dict):
            raise LinearError(_LINEAR_COMMENT_MISSING)
        evidence = _comment_create_evidence(comment, issue_id)
        recorder = self._self_echo_recorder
        if recorder is not None and recorder.comment_created is not None:
            await recorder.comment_created(evidence)
        return evidence

    async def record_issue_state_update(
        self,
        issue: dict[str, Any],
        *,
        issue_id: str,
        state_id: str,
    ) -> None:
        """Persist one host-owned nonterminal state callback marker from a receipt."""
        recorder = getattr(self, "_self_echo_recorder", None)
        if recorder is None or recorder.issue_state_updated is None:
            return
        evidence = _issue_state_update_evidence(
            issue,
            issue_id=issue_id,
            state_id=state_id,
        )
        if evidence.get("stateType") in TERMINAL_STATE_TYPES:
            return
        await recorder.issue_state_updated(evidence)

    async def create_attachment(
        self,
        issue_id: str,
        url: str,
        title: str,
        *,
        subtitle: str | None = None,
    ) -> dict[str, Any]:
        """Attach an external resource to an issue.

        Linear treats an issue and URL pair as idempotent, so a repeated call
        updates the visible attachment instead of creating a duplicate.
        """
        data = await self.query(
            """
            mutation CreateAttachment(
              $issue_id: String!,
              $url: String!,
              $title: String!,
              $subtitle: String
            ) {
              attachmentCreate(
                input: {
                  issueId: $issue_id,
                  url: $url,
                  title: $title,
                  subtitle: $subtitle
                }
              ) {
                success
                attachment { id url title subtitle createdAt updatedAt }
              }
            }
            """,
            issue_id=issue_id,
            url=url,
            title=title,
            subtitle=subtitle,
        )
        result = data.get("attachmentCreate")
        if not isinstance(result, dict) or not result.get("success"):
            raise LinearError(_LINEAR_ATTACHMENT_NOT_CREATED)
        attachment = result.get("attachment")
        if not isinstance(attachment, dict):
            raise LinearError(_LINEAR_ATTACHMENT_MISSING)
        return attachment

    async def find_issues_by_attachment_url(self, url: str) -> list[dict[str, Any]]:
        """Resolve an external URL back to its Linear issue attachments."""
        data = await self.query(
            """
            query AttachmentsForURL($url: String!) {
              attachmentsForURL(url: $url) {
                nodes {
                  id url title subtitle
                  issue {
                    id identifier title url
                    state { id name type }
                    project { id name }
                  }
                }
              }
            }
            """,
            url=url,
        )
        return _nodes(data, "attachmentsForURL")

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


def _comment_create_evidence(comment: dict[str, Any], issue_id: str) -> dict[str, Any]:
    """Normalize only response fields that prove the echo is the write we made."""
    comment_id = comment.get("id")
    created_at = comment.get("createdAt")
    updated_at = comment.get("updatedAt")
    issue = comment.get("issue")
    response_issue_id = (
        comment.get("issueId")
        if isinstance(comment.get("issueId"), str)
        else issue.get("id")
        if isinstance(issue, dict)
        else None
    )
    evidence = (comment_id, response_issue_id, created_at, updated_at)
    if not all(isinstance(value, str) and value for value in evidence):
        raise LinearError(_LINEAR_COMMENT_EVIDENCE_MISSING)
    if response_issue_id != issue_id:
        raise LinearError(_LINEAR_COMMENT_ISSUE_MISMATCH)
    return {
        **comment,
        "id": comment_id,
        "issueId": response_issue_id,
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def _issue_state_update_evidence(
    issue: dict[str, Any],
    *,
    issue_id: str,
    state_id: str,
) -> dict[str, Any]:
    """Normalize only response fields shared with a Linear Issue/update callback."""
    response_issue_id = issue.get("id")
    updated_at = issue.get("updatedAt")
    state = issue.get("state")
    response_state_id = state.get("id") if isinstance(state, dict) else None
    evidence = (response_issue_id, response_state_id, updated_at)
    if not all(isinstance(value, str) and value for value in evidence):
        raise LinearError(_LINEAR_ISSUE_STATE_EVIDENCE_MISSING)
    if response_issue_id != issue_id:
        raise LinearError(_LINEAR_ISSUE_STATE_ISSUE_MISMATCH)
    if response_state_id != state_id:
        raise LinearError(_LINEAR_ISSUE_STATE_TARGET_MISMATCH)
    return {
        "id": response_issue_id,
        "stateId": response_state_id,
        "updatedAt": updated_at,
        "stateType": state.get("type") if isinstance(state, dict) else None,
    }


def _nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearError(_LINEAR_CONNECTION_MISSING.format(key=key))
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearError(_LINEAR_NODES_MISSING.format(key=key))
    return [node for node in nodes if isinstance(node, dict)]
