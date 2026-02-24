"""Run log file writing."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pynchy.container_runner._serialization import _input_to_dict
from pynchy.types import ContainerInput, VolumeMount


def _write_run_log(
    *,
    logs_dir: Path,
    group_name: str,
    container_name: str,
    input_data: ContainerInput,
    container_args: list[str],
    mounts: list[VolumeMount],
    stderr: str,
    duration_ms: float,
    exit_code: int | None,
    timed_out: bool,
    output_event_count: int,
) -> None:
    """Write a timestamped log file for a container run."""
    ts = datetime.now(UTC).isoformat().replace(":", "-").replace(".", "-")
    log_file = logs_dir / f"container-{ts}.log"

    if timed_out:
        lines = [
            "=== Container Run Log (TIMEOUT) ===",
            f"Timestamp: {datetime.now(UTC).isoformat()}",
            f"Group: {group_name}",
            f"Container: {container_name}",
            f"Duration: {duration_ms:.0f}ms",
            f"Exit Code: {exit_code}",
            f"Output Events: {output_event_count}",
        ]
        log_file.write_text("\n".join(lines))
        return

    is_verbose = os.environ.get("LOG_LEVEL", "").lower() in ("debug", "trace")
    is_error = exit_code != 0

    lines = [
        "=== Container Run Log ===",
        f"Timestamp: {datetime.now(UTC).isoformat()}",
        f"Group: {group_name}",
        f"IsMain: {input_data.is_admin}",
        f"Duration: {duration_ms:.0f}ms",
        f"Exit Code: {exit_code}",
        f"Output Events: {output_event_count}",
        "",
    ]

    if is_verbose or is_error:
        lines.extend(
            [
                "=== Input ===",
                json.dumps(_input_to_dict(input_data), indent=2),
                "",
                "=== Container Args ===",
                " ".join(container_args),
                "",
                "=== Mounts ===",
                "\n".join(
                    f"{m.host_path} -> {m.container_path}{' (ro)' if m.readonly else ''}"
                    for m in mounts
                ),
                "",
                "=== Stderr ===",
                stderr,
            ]
        )
    else:
        lines.extend(
            [
                "=== Input Summary ===",
                f"Messages: {len(input_data.messages)} messages",
                f"Session ID: {input_data.session_id or 'new'}",
                "",
                "=== Mounts ===",
                "\n".join(f"{m.container_path}{' (ro)' if m.readonly else ''}" for m in mounts),
                "",
            ]
        )

    log_file.write_text("\n".join(lines))
