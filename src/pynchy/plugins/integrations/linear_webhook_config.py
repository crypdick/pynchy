"""Typed configuration for authenticated Linear webhook routes."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class _LinearWebhookConfigModel(BaseModel):
    model_config = {"extra": "forbid"}


class LinearWebhookRouteConfig(_LinearWebhookConfigModel):
    """Plugin-owned config for one Linear webhook subscription."""

    name: str
    workspace: str | None = None
    tool: str = "linear"
    secret_env: str = "LINEAR_WEBHOOK_SECRET"  # noqa: S105 - environment variable name, not a credential.
    organization_id: str | None = None
    timestamp_tolerance_seconds: int = 60
    max_body_bytes: int = 256 * 1024
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    @field_validator("name", "tool", "secret_env")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Linear webhook route text fields cannot be empty")
        return value

    @field_validator(
        "timestamp_tolerance_seconds",
        "max_body_bytes",
        "rate_limit_requests",
        "rate_limit_window_seconds",
    )
    @classmethod
    def validate_positive_limits(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Linear webhook limits must be positive")
        return value


class LinearPluginOptions(_LinearWebhookConfigModel):
    """Typed transport parser for ``[plugins.linear.options]``."""

    webhook_routes: tuple[LinearWebhookRouteConfig, ...] = ()
