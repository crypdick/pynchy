"""Exercise deterministic-runtime cleanup after every service has become ready."""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404, RUF100 - failure probe inspects its harness-owned Docker image.
from pathlib import Path

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
    original_wait_for_runtime = harness._wait_for_runtime
    ready_agent_image: str | None = None

    def fail_after_ready(*args: object) -> None:
        nonlocal ready_agent_image
        original_wait_for_runtime(*args)
        ready_agent_image = _ready_runtime_agent_image(root)
        raise RuntimeError(_EXPECTED_FAILURE)

    harness._wait_for_runtime = fail_after_ready
    try:
        harness.setup(spec)
    except RuntimeError as exc:
        if str(exc) != _EXPECTED_FAILURE:
            raise
    else:
        raise RuntimeError("Controlled runtime failure was not raised")
    finally:
        harness._wait_for_runtime = original_wait_for_runtime

    if harness._read_state(root) is not None:
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


def _ready_runtime_agent_image(root: Path) -> str:
    state = harness._read_state(root)
    if state is None:
        raise RuntimeError("Runtime state disappeared before the controlled failure")
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
    server_port = harness._available_port(set())
    gateway_port = harness._available_port({server_port})
    temporal_port = harness._available_port({server_port, gateway_port})
    return harness.RuntimeSpec(
        root=root,
        namespace=namespace,
        server_port=server_port,
        gateway_port=gateway_port,
        temporal_port=temporal_port,
    )


if __name__ == "__main__":
    main()
