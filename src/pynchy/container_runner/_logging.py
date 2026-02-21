"""Run log file writing and legacy output parsing."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pynchy.config import Settings
from pynchy.container_runner._serialization import _input_to_dict, _parse_container_output
from pynchy.logger import logger
from pynchy.types import ContainerInput, ContainerOutput, VolumeMount


def _write_run_log(
    *,
    logs_dir: Path,
    group_name: str,
    container_name: str,
    input_data: ContainerInput,
    container_args: list[str],
    mounts: list[VolumeMount],
    stdout: str,
    stderr: str,
    stdout_truncated: bool,
    stderr_truncated: bool,
    duration_ms: float,
    exit_code: int | None,
    timed_out: bool,
    had_streaming_output: bool,
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
            f"Had Streaming Output: {had_streaming_output}",
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
        f"Stdout Truncated: {stdout_truncated}",
        f"Stderr Truncated: {stderr_truncated}",
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
                f"=== Stderr{' (TRUNCATED)' if stderr_truncated else ''} ===",
                stderr,
                "",
                f"=== Stdout{' (TRUNCATED)' if stdout_truncated else ''} ===",
                stdout,
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


def _parse_final_output(
    stdout: str, container_name: str, stderr: str, duration_ms: float
) -> ContainerOutput:
    """Parse the last marker pair from accumulated stdout (legacy mode)."""
    start_idx = stdout.find(Settings.OUTPUT_START_MARKER)
    end_idx = stdout.find(Settings.OUTPUT_END_MARKER)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        json_str = stdout[start_idx + len(Settings.OUTPUT_START_MARKER) : end_idx].strip()
    else:
        # Fallback: last non-empty line
        lines = stdout.strip().splitlines()
        json_str = lines[-1] if lines else ""

    try:
        return _parse_container_output(json_str)
    except json.JSONDecodeError as exc:
        # Truncate long output to avoid flooding logs
        preview = json_str[:200] + "..." if len(json_str) > 200 else json_str
        logger.error(
            "Invalid JSON in container output",
            container=container_name,
            json_error=str(exc),
            preview=preview,
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Invalid JSON in container output: {exc}",
        )
    except KeyError as exc:
        logger.error(
            "Missing required field in container output",
            container=container_name,
            missing_key=str(exc),
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Missing required field in container output: {exc}",
        )
    except Exception as exc:
        logger.error(
            "Failed to parse container output",
            container=container_name,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Failed to parse container output: {exc}",
        )
