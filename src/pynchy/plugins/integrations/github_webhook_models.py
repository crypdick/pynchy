"""Typed GitHub webhook configuration and payload models."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# GitHub rejects webhook deliveries larger than this documented maximum, so this
# route accepts every payload GitHub can deliver. Keep docs/integrations/github.md
# in sync with this provider contract.
GITHUB_MAX_WEBHOOK_BODY_BYTES = 25 * 1024 * 1024


class _GitHubModel(BaseModel):
    model_config = {"extra": "ignore"}


class GitHubWebhookRouteConfig(_GitHubModel):
    """Plugin-owned configuration for one repository-to-workspace route."""

    model_config = {"extra": "forbid"}

    name: str
    workspace: str
    repository: str
    secret_env: str = "GITHUB_WEBHOOK_SECRET"  # noqa: S105 - environment variable name, not a credential.
    # NOTE: Update docs/integrations/github.md "Trust selected GitHub senders" if this changes.
    allowed_senders: tuple[str, ...] = ()
    max_body_bytes: int = GITHUB_MAX_WEBHOOK_BODY_BYTES
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    @field_validator("name", "workspace", "repository", "secret_env")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("GitHub webhook route text fields cannot be empty")
        return value

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if value.count("/") != 1 or any(not component for component in value.split("/")):
            raise ValueError("GitHub webhook repository must have owner/repository form")
        return value

    @field_validator("allowed_senders")
    @classmethod
    def validate_allowed_senders(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not sender.strip() for sender in value):
            raise ValueError("GitHub webhook sender allowlist cannot contain blank logins")
        normalized = tuple(sender.casefold() for sender in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("GitHub webhook sender allowlist cannot contain duplicates")
        return value

    @field_validator("max_body_bytes", "rate_limit_requests", "rate_limit_window_seconds")
    @classmethod
    def validate_positive_limits(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("GitHub webhook limits must be positive")
        return value

    @field_validator("max_body_bytes")
    @classmethod
    def validate_github_payload_limit(cls, value: int) -> int:
        if value > GITHUB_MAX_WEBHOOK_BODY_BYTES:
            raise ValueError("GitHub webhook body limit cannot exceed GitHub's 25 MiB payload cap")
        return value


class GitHubPluginOptions(_GitHubModel):
    """Typed transport parser for ``[plugins.github.options]``."""

    model_config = {"extra": "forbid"}

    webhook_routes: tuple[GitHubWebhookRouteConfig, ...] = ()


class _GitHubRepository(_GitHubModel):
    full_name: str


class _GitHubSender(_GitHubModel):
    login: str


class _GitHubPullRequest(_GitHubModel):
    number: int | None = None
    html_url: str | None = None
    mergeable: bool | None = None
    mergeable_state: str | None = None
    merged: bool = False
    updated_at: str | None = None


class _GitHubIssue(_GitHubModel):
    number: int
    pull_request: dict[str, object] | None = None
    updated_at: str | None = None


class _GitHubReview(_GitHubModel):
    state: str | None = None
    submitted_at: str | None = None


class _GitHubCheckPullRequest(_GitHubModel):
    number: int


class _GitHubCheckRun(_GitHubModel):
    name: str
    conclusion: str | None = None
    pull_requests: tuple[_GitHubCheckPullRequest, ...] = ()
    completed_at: str | None = None


class GitHubEnvelope(_GitHubModel):
    repository: _GitHubRepository
    sender: _GitHubSender | None = None
    action: str = ""
    number: int | None = None
    pull_request: _GitHubPullRequest | None = None
    issue: _GitHubIssue | None = None
    review: _GitHubReview | None = None
    check_run: _GitHubCheckRun | None = None
    changes: dict[str, object] = Field(default_factory=dict)

    def pull_request_number(self) -> int | None:
        if self.number is not None:
            return self.number
        return self.pull_request.number if self.pull_request is not None else None
