"""Built-in Google Setup plugin and public setup helpers."""

from pynchy.plugins.integrations.google_setup._oauth import run_oauth_flow
from pynchy.plugins.integrations.google_setup._paths import (
    GoogleSetupRuntime,
    configure_google_setup_runtime,
)
from pynchy.plugins.integrations.google_setup._plugin import GoogleMcpPlugin, GoogleSetupPlugin

__all__ = [
    "GoogleMcpPlugin",
    "GoogleSetupPlugin",
    "GoogleSetupRuntime",
    "configure_google_setup_runtime",
    "run_oauth_flow",
]
