"""Cross-runtime isolation coverage for the deterministic profile."""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404, RUF100 - tests manage only harness-owned local runtimes.
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ._helpers import (
    groups,
    messages,
    runtime_state,
    send_message,
    status,
    wait_for_ready,
    wait_for_response_count,
)

pytestmark = pytest.mark.runtime

_COPIED_CHECKOUT_EXCLUSIONS = {
    ".env",
    ".git",
    ".mypy_cache",
    ".new-feature",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".worktrees",
    "__pycache__",
    "config.toml",
    "groups",
    "litellm_config.yaml",
    "logs",
}


@pytest.mark.timeout(180)
def test_second_runtime_has_its_own_database_and_docker_namespace(tmp_path: Path) -> None:
    """A second checkout cannot read or clean up the first runtime's state."""
    primary_state = runtime_state()
    primary_namespace = _required_string(primary_state, "namespace")
    primary_agent_image = _required_string(primary_state, "agent_image")
    assert primary_agent_image.startswith(f"pynchy-runtime-agent:{primary_namespace}-")
    primary_jid = _runtime_jid(primary_state)
    response_text = _required_string(primary_state, "response_text")
    primary_marker = uuid4().hex
    primary_before = _response_count(messages(primary_state, primary_jid), response_text)
    send_message(primary_state, primary_jid, f"runtime isolation primary {primary_marker}")
    wait_for_response_count(primary_state, primary_jid, response_text, primary_before + 1)

    source_root = Path(__file__).resolve().parents[2]
    secondary_root = tmp_path / "secondary-pynchy"
    shutil.copytree(source_root, secondary_root, ignore=_ignore_runtime_artifacts)
    secondary_namespace = f"pynchy-runtime-isolation-{uuid4().hex[:10]}"
    environment = _secondary_environment()
    secondary_state_path = secondary_root / "data" / "pynchy-runtime" / "runtime.json"
    secondary_agent_image: str | None = None
    try:
        setup = subprocess.run(  # noqa: S603, RUF100 - fixed harness command with a test-owned checkout.
            [  # noqa: S607, RUF100 - uv is the repository's required Python runner.
                "uv",
                "run",
                "python",
                "scripts/runtime_harness.py",
                "--root",
                str(secondary_root),
                "--namespace",
                secondary_namespace,
                "setup",
            ],
            cwd=source_root,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=180,
        )
        assert setup.returncode == 0

        secondary_state = runtime_state(secondary_state_path)
        assert secondary_state["namespace"] == secondary_namespace
        assert secondary_state["namespace"] != primary_state["namespace"]
        assert secondary_state["network"] != primary_state["network"]
        secondary_agent_image = _required_string(secondary_state, "agent_image")
        assert secondary_agent_image.startswith(f"pynchy-runtime-agent:{secondary_namespace}-")
        assert secondary_agent_image != primary_agent_image
        wait_for_ready(secondary_state)

        secondary_jid = _runtime_jid(secondary_state)
        secondary_history = messages(secondary_state, secondary_jid)
        assert all(
            item.get("content") != f"runtime isolation primary {primary_marker}"
            for item in secondary_history
        )

        secondary_before = _response_count(secondary_history, response_text)
        secondary_marker = uuid4().hex
        send_message(
            secondary_state, secondary_jid, f"runtime isolation secondary {secondary_marker}"
        )
        secondary_history = wait_for_response_count(
            secondary_state,
            secondary_jid,
            response_text,
            secondary_before + 1,
        )
        assert any(
            item.get("content") == f"runtime isolation secondary {secondary_marker}"
            for item in secondary_history
        )
        _assert_docker_resource_exists("container", f"{secondary_namespace}-pynchy")
        _assert_docker_resource_exists("container", str(secondary_state["fake_container"]))
        _assert_docker_resource_exists("image", secondary_agent_image)
        assert status(primary_state)["service"]["status"] == "ok"
    finally:
        stop = subprocess.run(  # noqa: S603, RUF100 - fixed harness cleanup for the test-owned checkout.
            [  # noqa: S607, RUF100 - uv is the repository's required Python runner.
                "uv",
                "run",
                "python",
                "scripts/runtime_harness.py",
                "--root",
                str(secondary_root),
                "stop",
            ],
            cwd=source_root,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=90,
        )
        assert stop.returncode == 0

    _assert_docker_resource_is_gone("container", f"{secondary_namespace}-pynchy")
    _assert_docker_resource_is_gone("container", f"{secondary_namespace}-deterministic-openai")
    assert secondary_agent_image is not None
    _assert_docker_resource_is_gone("image", secondary_agent_image)
    _assert_docker_resource_exists("image", primary_agent_image)
    assert status(primary_state)["service"]["status"] == "ok"


def _ignore_runtime_artifacts(directory: str, names: list[str]) -> set[str]:
    if Path(directory).name == "data":
        return set(names) - {"defaults"}
    return {name for name in names if name in _COPIED_CHECKOUT_EXCLUSIONS}


def _secondary_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "GATEWAY__PORT",
        "NEW_FEATURE_TEMPORAL_PORT",
        "PYNCHY_RUNTIME_NAMESPACE",
        "SERVER__PORT",
    ):
        environment.pop(name, None)
    return environment


def _runtime_jid(state: dict[str, Any]) -> str:
    matching = [group.get("jid") for group in groups(state) if group.get("folder") == "pynchy"]
    assert matching == ["runtime:pynchy"]
    return "runtime:pynchy"


def _response_count(history: list[dict[str, Any]], response_text: str) -> int:
    return sum(
        item.get("sender_name") == "pynchy" and item.get("content") == response_text
        for item in history
    )


def _required_string(state: dict[str, Any], key: str) -> str:
    value = state.get(key)
    assert isinstance(value, str)
    return value


def _assert_docker_resource_exists(resource_type: str, name: str) -> None:
    result = subprocess.run(  # noqa: S603, RUF100 - name is generated by the harness namespace.
        [  # noqa: S607, RUF100 - Docker is a required local runtime executable.
            "docker",
            resource_type,
            "inspect",
            name,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0


def _assert_docker_resource_is_gone(resource_type: str, name: str) -> None:
    result = subprocess.run(  # noqa: S603, RUF100 - name is generated by the harness namespace.
        [  # noqa: S607, RUF100 - Docker is a required local runtime executable.
            "docker",
            resource_type,
            "inspect",
            name,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
