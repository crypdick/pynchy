"""Exercise deterministic-runtime cleanup after every service has become ready."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess  # noqa: S404, RUF100 - failure probe inspects its harness-owned Docker image.
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

from scripts import runtime_harness as harness

_EXPECTED_FAILURE = "controlled failure after deterministic runtime readiness"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    root = args.root.resolve()
    spec = _runtime_spec(root, args.namespace)
    ready_agent_image: str | None = None

    def fail_after_ready(ready_spec: harness.RuntimeSpec, _process: object) -> None:
        nonlocal ready_agent_image
        ready_agent_image = _wait_for_semantic_readiness(ready_spec)
        raise RuntimeError(_EXPECTED_FAILURE)

    with patch("scripts.runtime_harness._wait_for_runtime", side_effect=fail_after_ready):
        try:
            harness.setup(spec)
        except RuntimeError as exc:
            if str(exc) != _EXPECTED_FAILURE:
                raise
        else:
            raise RuntimeError("Controlled runtime failure was not raised")

    if spec.state_path.exists():
        raise RuntimeError("Failed runtime setup left runtime.json behind")
    if ready_agent_image is None:
        raise RuntimeError("Controlled runtime failure did not observe the built agent image")

    result_path = root / "data" / "pynchy-runtime" / "partial-start-result.json"
    result_path.write_text(
        json.dumps(
            {
                "agent_image": ready_agent_image,
                "server_port": spec.server_port,
                "gateway_port": spec.gateway_port,
                "temporal_port": spec.temporal_port,
            }
        ),
        encoding="utf-8",
    )


def _wait_for_semantic_readiness(spec: harness.RuntimeSpec) -> str:
    """Use the public status endpoint before triggering the injected failure."""
    deadline = time.monotonic() + 90
    status_url = f"http://127.0.0.1:{spec.server_port}/status"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=2) as response:  # noqa: S310, RUF100 - fixed loopback status endpoint.
                if response.status == 200:
                    status = json.loads(response.read())
                    if harness.is_runtime_ready(status):
                        return _ready_runtime_agent_image(spec.state_path)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Runtime did not become semantically ready at {status_url}")


def _ready_runtime_agent_image(state_path: Path) -> str:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise TypeError("Runtime state must be a JSON object")
    image = state.get("agent_image")
    if not isinstance(image, str):
        raise TypeError("Runtime state is missing the namespace-scoped agent image")
    result = subprocess.run(  # noqa: S603, RUF100 - image comes from this harness-owned state file.
        [  # noqa: S607, RUF100 - Docker is the deterministic runtime's required executable.
            "docker",
            "image",
            "inspect",
            image,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Runtime agent image was not built before failure: {image}")
    return image


def _runtime_spec(root: Path, namespace: str) -> harness.RuntimeSpec:
    server_port = _available_port(set())
    gateway_port = _available_port({server_port})
    temporal_port = _available_port({server_port, gateway_port})
    return harness.RuntimeSpec(
        root=root,
        namespace=namespace,
        server_port=server_port,
        gateway_port=gateway_port,
        temporal_port=temporal_port,
    )


def _available_port(excluded: set[int]) -> int:
    """Allocate a distinct ephemeral loopback port outside the harness internals."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.bind(("127.0.0.1", 0))
            port = int(connection.getsockname()[1])
        if port not in excluded:
            return port


if __name__ == "__main__":
    main()
