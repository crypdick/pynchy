"""Container runner — spawns agent execution in Apple Container.

Port of src/container-runner.ts. Spawns subprocesses, writes JSON input to stdin,
parses streaming output using sentinel markers, manages activity-based timeouts,
and writes log files.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pynchy.config import (
    CONTAINER_IMAGE,
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_TIMEOUT,
    DATA_DIR,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    OUTPUT_END_MARKER,
    OUTPUT_START_MARKER,
    PROJECT_ROOT,
)
from pynchy.logger import logger
from pynchy.mount_security import validate_additional_mounts
from pynchy.runtime import get_runtime
from pynchy.types import ContainerInput, ContainerOutput, RegisteredGroup, VolumeMount

# ---------------------------------------------------------------------------
# Serialization helpers (camelCase ↔ snake_case boundary)
# ---------------------------------------------------------------------------


def _input_to_dict(input_data: ContainerInput) -> dict[str, Any]:
    """Convert ContainerInput to dict for the Python agent-runner."""
    d: dict[str, Any] = {
        "prompt": input_data.prompt,
        "group_folder": input_data.group_folder,
        "chat_jid": input_data.chat_jid,
        "is_main": input_data.is_main,
    }
    if input_data.session_id is not None:
        d["session_id"] = input_data.session_id
    if input_data.is_scheduled_task:
        d["is_scheduled_task"] = True
    return d


def _parse_container_output(json_str: str) -> ContainerOutput:
    """Parse JSON from the Python agent-runner into ContainerOutput."""
    data = json.loads(json_str)
    return ContainerOutput(
        status=data["status"],
        result=data.get("result"),
        new_session_id=data.get("new_session_id"),
        error=data.get("error"),
    )


# ---------------------------------------------------------------------------
# File preparation helpers
# ---------------------------------------------------------------------------


def _sync_skills(session_dir: Path) -> None:
    """Copy container/skills/ into the session's .claude/skills/ directory."""
    skills_src = PROJECT_ROOT / "container" / "skills"
    skills_dst = session_dir / "skills"
    if not skills_src.exists():
        return
    for skill_dir in skills_src.iterdir():
        if not skill_dir.is_dir():
            continue
        dst_dir = skills_dst / skill_dir.name
        dst_dir.mkdir(parents=True, exist_ok=True)
        for f in skill_dir.iterdir():
            if f.is_file():
                shutil.copy2(f, dst_dir / f.name)


def _read_oauth_token() -> str | None:
    """Read the OAuth access token from Claude Code's credentials file."""
    creds_file = Path.home() / ".claude" / ".credentials.json"
    if not creds_file.exists():
        return None
    try:
        data = json.loads(creds_file.read_text())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, OSError):
        return None


def _write_env_file() -> Path | None:
    """Write credential env vars for the container. Returns env dir or None.

    Sources (in priority order):
    1. .env file in project root (explicit ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN)
    2. Claude Code's OAuth token from ~/.claude/.credentials.json (auto-refreshed)
    """
    env_dir = DATA_DIR / "env"
    env_dir.mkdir(parents=True, exist_ok=True)

    # Try .env file first
    filtered: list[str] = []
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        content = env_file.read_text()
        allowed_vars = ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
        filtered = [
            line
            for line in content.splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and any(line.strip().startswith(f"{v}=") for v in allowed_vars)
        ]

    # Fallback: read OAuth token from Claude Code credentials
    if not filtered:
        token = _read_oauth_token()
        if token:
            filtered = [f"CLAUDE_CODE_OAUTH_TOKEN={token}"]
            logger.debug("Using OAuth token from Claude Code credentials")

    if not filtered:
        return None

    (env_dir / "env").write_text("\n".join(filtered) + "\n")
    return env_dir


def _write_settings_json(session_dir: Path) -> None:
    """Write Claude Code settings.json if it doesn't exist."""
    settings_file = session_dir / "settings.json"
    if settings_file.exists():
        return
    settings = {
        "env": {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
        },
    }
    settings_file.write_text(json.dumps(settings, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Mount building
# ---------------------------------------------------------------------------


def _build_volume_mounts(group: RegisteredGroup, is_main: bool) -> list[VolumeMount]:
    """Build the mount list for a container invocation."""
    mounts: list[VolumeMount] = []

    group_dir = GROUPS_DIR / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    if is_main:
        mounts.append(VolumeMount(str(PROJECT_ROOT), "/workspace/project", readonly=False))
        mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))
    else:
        mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))
        global_dir = GROUPS_DIR / "global"
        if global_dir.exists():
            mounts.append(VolumeMount(str(global_dir), "/workspace/global", readonly=True))

    # Per-group Claude sessions directory (isolated from other groups)
    session_dir = DATA_DIR / "sessions" / group.folder / ".claude"
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_settings_json(session_dir)
    _sync_skills(session_dir)
    mounts.append(VolumeMount(str(session_dir), "/home/agent/.claude", readonly=False))

    # Per-group IPC namespace
    group_ipc_dir = DATA_DIR / "ipc" / group.folder
    for sub in ("messages", "tasks", "input"):
        (group_ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append(VolumeMount(str(group_ipc_dir), "/workspace/ipc", readonly=False))

    # Environment file directory
    env_dir = _write_env_file()
    if env_dir is not None:
        mounts.append(VolumeMount(str(env_dir), "/workspace/env-dir", readonly=True))

    # Agent-runner source (read-only, Python source for container)
    agent_runner_src = PROJECT_ROOT / "container" / "agent_runner" / "src"
    mounts.append(VolumeMount(str(agent_runner_src), "/app/src", readonly=True))

    # Additional mounts validated against external allowlist
    if group.container_config and group.container_config.additional_mounts:
        validated = validate_additional_mounts(
            group.container_config.additional_mounts, group.name, is_main
        )
        for m in validated:
            mounts.append(
                VolumeMount(
                    host_path=str(m["hostPath"]),
                    container_path=str(m["containerPath"]),
                    readonly=bool(m["readonly"]),
                )
            )

    return mounts


def _build_container_args(mounts: list[VolumeMount], container_name: str) -> list[str]:
    """Build CLI args for `container run`."""
    args = ["run", "-i", "--rm", "--name", container_name]
    for m in mounts:
        if m.readonly:
            args.extend(
                [
                    "--mount",
                    f"type=bind,source={m.host_path},target={m.container_path},readonly",
                ]
            )
        else:
            args.extend(["-v", f"{m.host_path}:{m.container_path}"])
    args.append(CONTAINER_IMAGE)
    return args


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


async def _graceful_stop(proc: asyncio.subprocess.Process, container_name: str) -> None:
    """Stop container gracefully with 15s timeout, fallback to kill."""
    try:
        stop_proc = await asyncio.create_subprocess_exec(
            get_runtime().cli,
            "stop",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(stop_proc.wait(), timeout=15.0)
        except TimeoutError:
            logger.warning(
                "Graceful stop timed out, force killing",
                container=container_name,
            )
            proc.kill()
    except Exception:
        logger.warning("Graceful stop failed, force killing", container=container_name)
        proc.kill()


# ---------------------------------------------------------------------------
# Log writing
# ---------------------------------------------------------------------------


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
        f"IsMain: {input_data.is_main}",
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
                f"Prompt length: {len(input_data.prompt)} chars",
                f"Session ID: {input_data.session_id or 'new'}",
                "",
                "=== Mounts ===",
                "\n".join(f"{m.container_path}{' (ro)' if m.readonly else ''}" for m in mounts),
                "",
            ]
        )

    log_file.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Legacy output parsing (no on_output callback)
# ---------------------------------------------------------------------------


def _parse_final_output(
    stdout: str, container_name: str, stderr: str, duration_ms: float
) -> ContainerOutput:
    """Parse the last marker pair from accumulated stdout (legacy mode)."""
    start_idx = stdout.find(OUTPUT_START_MARKER)
    end_idx = stdout.find(OUTPUT_END_MARKER)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        json_str = stdout[start_idx + len(OUTPUT_START_MARKER) : end_idx].strip()
    else:
        # Fallback: last non-empty line
        lines = stdout.strip().splitlines()
        json_str = lines[-1] if lines else ""

    try:
        return _parse_container_output(json_str)
    except Exception as exc:
        logger.error(
            "Failed to parse container output",
            container=container_name,
            error=str(exc),
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Failed to parse container output: {exc}",
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

OnProcess = Any  # (proc, container_name) -> None
OnOutput = Any  # async (output: ContainerOutput) -> None


async def run_container_agent(
    group: RegisteredGroup,
    input_data: ContainerInput,
    on_process: OnProcess,
    on_output: OnOutput | None = None,
) -> ContainerOutput:
    """Spawn a container agent, stream output, manage timeouts, and return result.

    Args:
        group: The registered group configuration.
        input_data: Input payload for the agent-runner.
        on_process: Callback invoked with (proc, container_name) after spawn.
        on_output: If provided, called for each streamed output marker pair.
                   Enables streaming mode. Without it, uses legacy mode.

    Returns:
        ContainerOutput with the final status.
    """
    start_time = time.monotonic()
    loop = asyncio.get_running_loop()

    group_dir = GROUPS_DIR / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    mounts = _build_volume_mounts(group, input_data.is_main)
    safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in group.folder)
    container_name = f"pynchy-{safe_name}-{int(time.time() * 1000)}"
    container_args = _build_container_args(mounts, container_name)

    logger.info(
        "Spawning container agent",
        group=group.name,
        container=container_name,
        mount_count=len(mounts),
        is_main=input_data.is_main,
    )

    logs_dir = GROUPS_DIR / group.folder / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        get_runtime().cli,
        *container_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    on_process(proc, container_name)

    # Write input JSON and close stdin (Apple Container needs EOF to flush pipe)
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(_input_to_dict(input_data)).encode())
    proc.stdin.close()

    # --- State ---
    stdout_buf = ""
    stderr_buf = ""
    stdout_truncated = False
    stderr_truncated = False
    timed_out = False
    had_streaming_output = False
    new_session_id: str | None = None
    parse_buffer = ""

    # --- Timeout management ---
    config_timeout = (
        group.container_config.timeout
        if group.container_config and group.container_config.timeout
        else CONTAINER_TIMEOUT
    )
    # Grace period: hard timeout must be at least IDLE_TIMEOUT + 30s
    timeout_secs = max(config_timeout, IDLE_TIMEOUT + 30.0)
    timeout_handle: asyncio.TimerHandle | None = None

    def kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        logger.error(
            "Container timeout, stopping gracefully",
            group=group.name,
            container=container_name,
        )
        asyncio.ensure_future(_graceful_stop(proc, container_name))

    def reset_timeout() -> None:
        nonlocal timeout_handle
        if timeout_handle is not None:
            timeout_handle.cancel()
        timeout_handle = loop.call_later(timeout_secs, kill_on_timeout)

    reset_timeout()

    # --- Stdout reader ---
    async def read_stdout() -> None:
        nonlocal stdout_buf, stdout_truncated, parse_buffer, had_streaming_output, new_session_id
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            text = chunk.decode(errors="replace")

            # Accumulate for logging (with truncation)
            if not stdout_truncated:
                remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stdout_buf)
                if len(text) > remaining:
                    stdout_buf += text[:remaining]
                    stdout_truncated = True
                    logger.warning(
                        "Container stdout truncated",
                        group=group.name,
                        size=len(stdout_buf),
                    )
                else:
                    stdout_buf += text

            # Stream-parse for output markers
            if on_output is not None:
                parse_buffer += text
                while True:
                    start_idx = parse_buffer.find(OUTPUT_START_MARKER)
                    if start_idx == -1:
                        break
                    end_idx = parse_buffer.find(OUTPUT_END_MARKER, start_idx)
                    if end_idx == -1:
                        break  # Incomplete pair, wait for more data

                    json_str = parse_buffer[start_idx + len(OUTPUT_START_MARKER) : end_idx].strip()
                    parse_buffer = parse_buffer[end_idx + len(OUTPUT_END_MARKER) :]

                    try:
                        parsed = _parse_container_output(json_str)
                        if parsed.new_session_id:
                            new_session_id = parsed.new_session_id
                        had_streaming_output = True
                        reset_timeout()
                        await on_output(parsed)
                    except Exception as exc:
                        logger.warning(
                            "Failed to parse streamed output chunk",
                            group=group.name,
                            error=str(exc),
                        )

    # --- Stderr reader ---
    async def read_stderr() -> None:
        nonlocal stderr_buf, stderr_truncated
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                break
            text = chunk.decode(errors="replace")

            lines = text.strip().splitlines()
            for line in lines:
                if line:
                    logger.debug(line, container=group.folder)

            if not stderr_truncated:
                remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stderr_buf)
                if len(text) > remaining:
                    stderr_buf += text[:remaining]
                    stderr_truncated = True
                    logger.warning(
                        "Container stderr truncated",
                        group=group.name,
                        size=len(stderr_buf),
                    )
                else:
                    stderr_buf += text

    # --- Run readers concurrently, then wait for process exit ---
    await asyncio.gather(read_stdout(), read_stderr())
    exit_code = await proc.wait()

    # Cancel timeout
    if timeout_handle is not None:
        timeout_handle.cancel()

    duration_ms = (time.monotonic() - start_time) * 1000

    # --- Write log ---
    _write_run_log(
        logs_dir=logs_dir,
        group_name=group.name,
        container_name=container_name,
        input_data=input_data,
        container_args=container_args,
        mounts=mounts,
        stdout=stdout_buf,
        stderr=stderr_buf,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_ms=duration_ms,
        exit_code=exit_code,
        timed_out=timed_out,
        had_streaming_output=had_streaming_output,
    )

    # --- Determine result ---

    if timed_out:
        if had_streaming_output:
            logger.info(
                "Container timed out after output (idle cleanup)",
                group=group.name,
                container=container_name,
                duration_ms=duration_ms,
            )
            return ContainerOutput(status="success", result=None, new_session_id=new_session_id)

        logger.error(
            "Container timed out with no output",
            group=group.name,
            container=container_name,
            duration_ms=duration_ms,
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container timed out after {config_timeout:.0f}s",
        )

    if exit_code != 0:
        logger.error(
            "Container exited with error",
            group=group.name,
            code=exit_code,
            duration_ms=duration_ms,
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container exited with code {exit_code}: {stderr_buf[-200:]}",
        )

    # Streaming mode: result already delivered via on_output callbacks
    if on_output is not None:
        logger.info(
            "Container completed (streaming mode)",
            group=group.name,
            duration_ms=duration_ms,
            new_session_id=new_session_id,
        )
        return ContainerOutput(status="success", result=None, new_session_id=new_session_id)

    # Legacy mode: parse final output from stdout
    return _parse_final_output(stdout_buf, container_name, stderr_buf, duration_ms)


# ---------------------------------------------------------------------------
# Snapshot helpers (written before container launch for agent to read)
# ---------------------------------------------------------------------------


def write_tasks_snapshot(
    folder: str,
    is_main: bool,
    tasks: list[dict[str, Any]],
) -> None:
    """Write current_tasks.json to the group's IPC directory."""
    group_ipc_dir = DATA_DIR / "ipc" / folder
    group_ipc_dir.mkdir(parents=True, exist_ok=True)

    # Main sees all tasks, others only see their own
    filtered = tasks if is_main else [t for t in tasks if t.get("groupFolder") == folder]
    (group_ipc_dir / "current_tasks.json").write_text(json.dumps(filtered, indent=2))


def write_groups_snapshot(
    folder: str,
    is_main: bool,
    groups: list[dict[str, Any]],
    registered_jids: set[str],
) -> None:
    """Write available_groups.json to the group's IPC directory."""
    group_ipc_dir = DATA_DIR / "ipc" / folder
    group_ipc_dir.mkdir(parents=True, exist_ok=True)

    # Main sees all groups; others see nothing (they can't activate groups)
    visible = groups if is_main else []
    payload = {
        "groups": visible,
        "lastSync": datetime.now(UTC).isoformat(),
    }
    (group_ipc_dir / "available_groups.json").write_text(json.dumps(payload, indent=2))
