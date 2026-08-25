"""Control-plane configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config.api import OpsConfig, ServerConfig


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"port": 0}, "server.port must be between 1 and 65535"),
        ({"port": 65536}, "server.port must be between 1 and 65535"),
        ({"rate_limit_requests": 0}, "server rate-limit values must be positive"),
        ({"rate_limit_window_seconds": -1}, "server rate-limit values must be positive"),
        ({"host": "  "}, "server host and auth_token_env must not be empty"),
        ({"auth_token_env": "  "}, "server host and auth_token_env must not be empty"),
    ],
)
def test_server_config_rejects_unsafe_listener_values(
    values: dict[str, int | str],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ServerConfig(**values)


def test_server_config_accepts_the_smallest_safe_listener_values() -> None:
    config = ServerConfig(
        host="localhost",
        port=1,
        rate_limit_requests=1,
        rate_limit_window_seconds=1,
    )

    assert config.port == 1


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"ssh_host": "host;rm"}, "safe SSH host alias"),
        ({"namespace": "Pynchy"}, "Kubernetes DNS label"),
    ],
)
def test_ops_config_rejects_command_shaped_target_values(
    values: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        OpsConfig(**values)
