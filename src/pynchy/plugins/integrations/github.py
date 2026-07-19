"""Built-in GitHub webhook plugin for deterministic PR status notifications."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pluggy

from pynchy.plugins.integrations.github_webhooks import github_webhook_routes

if TYPE_CHECKING:
    from pynchy.plugins.webhooks import WebhookRoute

hookimpl = pluggy.HookimplMarker("pynchy")


class GitHubWebhookPlugin:
    """Expose explicitly configured GitHub repository webhook routes."""

    @hookimpl
    def pynchy_webhook_routes(self) -> tuple[WebhookRoute, ...]:
        """Register configured repository-to-workspace routes at HTTP startup."""
        return github_webhook_routes()
