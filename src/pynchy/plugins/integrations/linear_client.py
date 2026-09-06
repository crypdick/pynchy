"""Typed GraphQL client shared by Linear MCP tools and operational canaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import (
    AsyncIterator,
)
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast, runtime_checkable

import aiohttp

from pynchy.plugins.integrations.linear_connections import (
    LinearIssueSummary,
    issue_connection_nodes,
    issue_connection_query,
)
from pynchy.plugins.integrations.linear_errors import LinearError
from pynchy.plugins.integrations.linear_mutation_effects import (
    LinearSelfEchoRecorder,
    LinearWebhookEffectAttempt,
)
from pynchy.plugins.integrations.linear_webhook_evidence import (
    comment_mutation_intent,
    comment_webhook_evidence,
    issue_state_webhook_evidence,
    normalize_comment_create_response,
    normalize_issue_state_update_response,
)
from pynchy.webhook_effects import WebhookEffectScope

LINEAR_API_URL = "https://api.linear.app/graphql"
_LINEAR_DATA_OBJECT_MISSING = "Linear response did not include a data object"
_LINEAR_ISSUE_NOT_CREATED = "Linear did not create the issue"
_LINEAR_ISSUE_CREATE_ISSUE_MISSING = "Linear issueCreate response did not include an issue"
_LINEAR_ISSUE_DELETE_FAILED = "Linear did not delete the issue"
_LINEAR_COMMENT_NOT_CREATED = "Linear did not create the comment"
_LINEAR_COMMENT_MISSING = "Linear commentCreate response did not include a comment"
_LINEAR_ATTACHMENT_NOT_CREATED = "Linear did not create the attachment"
_LINEAR_ATTACHMENT_MISSING = "Linear attachmentCreate response did not include an attachment"
_LINEAR_ISSUE_NOT_FOUND = "Entity not found: Issue"
_LINEAR_CONNECTION_MISSING = "Linear response did not include {key}"
_LINEAR_NODES_MISSING = "Linear response did not include {key}.nodes"
_TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})
_READ_RETRY_DELAYS = (0.25, 0.5)


@runtime_checkable
class LinearQueryClient(Protocol):
    """Minimal Linear boundary shared by mutation helpers and query-only clients."""

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        """Run a Linear GraphQL operation."""


class _AuthorizationSecret(str):
    """Authorization credential whose diagnostic representation is redacted."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted Linear authorization>"


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

    async def _query_once(
        self,
        payload: dict[str, object],
        headers: dict[str, str],
    ) -> tuple[int, Any | None]:
        session = cast("Any", self._session)
        async with session.post(self._endpoint, json=payload, headers=headers) as response:
            status = response.status if isinstance(response.status, int) else 200
            if status in _TRANSIENT_HTTP_STATUSES:
                return status, None
            try:
                return status, await response.json()
            except (
                aiohttp.ContentTypeError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as exc:
                raise LinearError(f"Linear request failed with HTTP {status}") from exc

    @asynccontextmanager
    async def webhook_effect(
        self,
        event_type: str,
        event_action: str,
        subject_id: str,
        *,
        intent_fingerprint: str | None = None,
    ) -> AsyncIterator[LinearWebhookEffectAttempt]:
        """Hold matching callbacks until this mutation has an exact outcome."""
        # Lightweight query fakes may inherit LinearClient without invoking its
        # network constructor. They have no host-owned recorder, so correlation
        # must remain the same deliberate no-op used by other query-only clients.
        recorder = getattr(self, "_self_echo_recorder", None)
        if recorder is None:
            yield LinearWebhookEffectAttempt(None, None)
            return
        scope = WebhookEffectScope(
            provider="linear",
            account=recorder.account_name,
            event_type=event_type,
            event_action=event_action,
            subject_id=subject_id,
            intent_fingerprint=intent_fingerprint,
        )
        effect_id = await recorder.begin(scope)
        await recorder.mark_executing(effect_id)
        attempt = LinearWebhookEffectAttempt(recorder, effect_id)
        try:
            yield attempt
        finally:
            if not attempt.resolved:
                await recorder.mark_outcome_unknown(effect_id)

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        payload: dict[str, object] = {"query": query, "variables": variables}
        headers: dict[str, str] = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }
        read_only = query.lstrip().startswith("query")
        for attempt in range(len(_READ_RETRY_DELAYS) + 1):  # pragma: no branch
            try:
                status, body = await self._query_once(payload, headers)
            except (aiohttp.ClientError, TimeoutError) as exc:
                if read_only and attempt < len(_READ_RETRY_DELAYS):
                    await asyncio.sleep(_READ_RETRY_DELAYS[attempt])
                    continue
                raise LinearError("Linear request failed") from exc
            if body is None:
                if read_only and attempt < len(_READ_RETRY_DELAYS):
                    await asyncio.sleep(_READ_RETRY_DELAYS[attempt])
                    continue
                raise LinearError(f"Linear request failed with HTTP {status}")
            break

        if errors := body.get("errors"):
            messages = "; ".join(str(error.get("message", error)) for error in errors)
            raise LinearError(messages)
        if not 200 <= status < 300:
            # Converting HTTP failures here prevents aiohttp tracebacks from
            # rendering the Linear authorization credential.
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
    ) -> list[LinearIssueSummary]:
        return await self._query_issues(team_id=team_id, first=first)

    async def search_issues(
        self,
        query: str,
        *,
        team_id: str | None = None,
        first: int = 50,
    ) -> list[LinearIssueSummary]:
        """Find issues whose titles contain the requested text."""
        return await self._query_issues(title_query=query, team_id=team_id, first=first)

    async def _query_issues(
        self,
        *,
        title_query: str | None = None,
        team_id: str | None = None,
        first: int,
    ) -> list[LinearIssueSummary]:
        variable_definitions = ""
        filters: list[str] = []
        variables: dict[str, object] = {"first": first}
        if title_query is not None:
            variable_definitions += ", $titleQuery: String!"
            filters.append("title: { containsIgnoreCase: $titleQuery }")
            variables["titleQuery"] = title_query
        if team_id:
            variable_definitions += ", $teamId: ID!"
            filters.append("team: { id: { eq: $teamId } }")
            variables["teamId"] = team_id
        issue_filter = f", filter: {{ {' '.join(filters)} }}" if filters else ""
        operation = "SearchIssues" if title_query is not None else "ListIssues"
        data = await self.query(
            issue_connection_query(operation, variable_definitions, issue_filter),
            **variables,
        )
        return issue_connection_nodes(data)

    async def create_issue(  # noqa: PLR0913 - Linear issue creation follows the API field set.
        self,
        *,
        team_id: str,
        title: str,
        description: str | None = None,
        project_id: str | None = None,
        state_id: str | None = None,
        label_ids: list[str] | None = None,
        priority: int | None = None,
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
            "priority": priority,
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
                    id identifier title description url updatedAt archivedAt
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

    async def list_issue_comments(self, issue_id: str, *, first: int = 100) -> list[dict[str, Any]]:
        """Return a bounded comment read for one issue reconciliation attempt."""
        data = await self.query(
            """
            query ListIssueComments($issue_id: String!, $first: Int!) {
              issue(id: $issue_id) {
                comments(first: $first) {
                  nodes { id body createdAt updatedAt issue { id } }
                }
              }
            }
            """,
            issue_id=issue_id,
            first=first,
        )
        issue = data.get("issue")
        if not isinstance(issue, dict):
            raise LinearError("Linear issue response was not an object")
        comments = issue.get("comments")
        if not isinstance(comments, dict):
            raise LinearError("Linear issue response did not include comments")
        nodes = comments.get("nodes")
        if not isinstance(nodes, list):
            raise LinearError("Linear issue response did not include comments.nodes")
        return [normalize_comment_create_response(comment, issue_id) for comment in nodes]

    async def create_comment(self, issue_id: str, body: str) -> dict[str, Any]:
        """Add an ordinary comment and retain its exact provider revision evidence."""
        async with self.webhook_effect(
            "Comment",
            "create",
            issue_id,
            intent_fingerprint=comment_mutation_intent(issue_id, body),
        ) as effect:
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
                await effect.fail()
                raise LinearError(_LINEAR_COMMENT_NOT_CREATED)
            comment = result.get("comment")
            if not isinstance(comment, dict):
                raise LinearError(_LINEAR_COMMENT_MISSING)
            response = normalize_comment_create_response(comment, issue_id)
            account_name = effect.account_name
            await effect.confirm(
                comment_webhook_evidence(
                    account_name,
                    comment_id=response["id"],
                    issue_id=response["issueId"],
                    revision=response["updatedAt"],
                )
                if account_name is not None
                else None
            )
            return response

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


@asynccontextmanager
async def linear_webhook_effect(
    client: LinearQueryClient,
    event_type: str,
    event_action: str,
    subject_id: str,
    *,
    intent_fingerprint: str | None = None,
) -> AsyncIterator[LinearWebhookEffectAttempt]:
    """Use durable correlation for host Linear clients and a no-op for query stubs."""
    if isinstance(client, LinearClient):
        async with client.webhook_effect(
            event_type,
            event_action,
            subject_id,
            intent_fingerprint=intent_fingerprint,
        ) as effect:
            yield effect
        return
    yield LinearWebhookEffectAttempt(None, None)


async def confirm_issue_state_effect(
    effect: LinearWebhookEffectAttempt,
    issue: dict[str, Any],
    *,
    issue_id: str,
    state_id: str,
) -> None:
    """Validate and commit exact Issue/update response evidence when enabled."""
    account_name = effect.account_name
    if account_name is None:
        return
    response = normalize_issue_state_update_response(
        issue,
        issue_id=issue_id,
        state_id=state_id,
    )
    await effect.confirm(
        issue_state_webhook_evidence(
            account_name,
            issue_id=response["id"],
            state_id=response["stateId"],
            revision=response["updatedAt"],
        )
    )


def _nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearError(_LINEAR_CONNECTION_MISSING.format(key=key))
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearError(_LINEAR_NODES_MISSING.format(key=key))
    return [node for node in nodes if isinstance(node, dict)]
