"""GitHub route construction and workspace attachment validation."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast

from pynchy.plugins.api import WebhookRoute
from pynchy.plugins.integrations.github_webhook_linear import prepare_github_webhook_event
from pynchy.plugins.integrations.github_webhooks import parse_github_webhook

if TYPE_CHECKING:
    from pynchy.plugins.integrations.github_webhook_models import GitHubWebhookRouteConfig


def github_webhook_routes(
    configs: tuple[object, ...],
    resolve_workspace_config: object,
) -> tuple[WebhookRoute, ...]:
    """Build explicitly mapped GitHub routes from resolved route configuration."""
    return tuple(
        WebhookRoute(
            provider="github",
            name=(typed_config := cast("GitHubWebhookRouteConfig", config)).name,
            workspace=typed_config.workspace,
            secret_env=typed_config.secret_env,
            parse=partial(parse_github_webhook, config=typed_config),
            validate_workspace=partial(
                _validate_repository_attachment,
                config=config,
                resolve_workspace_config=resolve_workspace_config,
            ),
            max_body_bytes=typed_config.max_body_bytes,
            rate_limit_requests=typed_config.rate_limit_requests,
            rate_limit_window_seconds=typed_config.rate_limit_window_seconds,
            prepare_event=partial(prepare_github_webhook_event, config=typed_config),
            routes_conversations=True,
        )
        for config in configs
    )


def _validate_repository_attachment(
    workspace: object,
    *,
    config: object,
    resolve_workspace_config: object,
) -> str | None:
    workspace_folder = getattr(workspace, "folder", None)
    if not isinstance(workspace_folder, str):
        return "does not have a workspace folder"
    if not callable(resolve_workspace_config):
        return "has no workspace configuration resolver"
    resolved = resolve_workspace_config(workspace_folder)
    if resolved is None:
        return "does not resolve to a configured workspace"
    repositories = getattr(resolved, "repo", ())
    repository = getattr(config, "repository", None)
    if not isinstance(repository, str):
        return "has no repository configuration"
    if any(repository.casefold() == configured.casefold() for configured in repositories):
        return None
    return f"does not attach repository {repository!r}"
