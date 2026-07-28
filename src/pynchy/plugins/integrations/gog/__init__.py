"""Host-only Google Workspace actions backed by the Gog CLI."""

from pynchy.plugins.integrations.gog._client import GogClient, GogError, create_gog_client
from pynchy.plugins.integrations.gog._config import GogConfig, GogRuntime, configure_gog_runtime
from pynchy.plugins.integrations.gog._plugin import GOG_HOST_ACTIONS, GogWorkspacePlugin

__all__ = [
    "GOG_HOST_ACTIONS",
    "GogClient",
    "GogConfig",
    "GogError",
    "GogRuntime",
    "GogWorkspacePlugin",
    "configure_gog_runtime",
    "create_gog_client",
]
