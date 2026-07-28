"""Composition adapter for host-owned messaging-source health."""

from __future__ import annotations

from typing import cast

from pynchy.config.api import get_settings
from pynchy.host.personal_messaging_health import (
    PERSONAL_PROVIDERS,
    PersonalProvider,
    personal_provider_for,
    project_personal_source,
)
from pynchy.state.api import get_latest_inbound_timestamp


class SourceHealthProjection:
    """Expose aggregate-only source health to the IPC handler."""

    @staticmethod
    def configured_connections() -> dict[str, str]:
        return {name: connection.type for name, connection in get_settings().connections.items()}

    personal_providers = staticmethod(lambda: PERSONAL_PROVIDERS)
    personal_provider_for = staticmethod(personal_provider_for)
    get_latest_inbound_timestamp = staticmethod(get_latest_inbound_timestamp)

    @staticmethod
    async def project_personal_source(provider: str) -> dict[str, object]:
        source_health = get_settings().messaging_source_health
        return await project_personal_source(
            cast("PersonalProvider", provider),
            data_dir=source_health.data_dir,
            stale_after_hours=source_health.stale_after_hours,
        )
