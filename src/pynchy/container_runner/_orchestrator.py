"""Main entry point — spawns container agent, manages lifecycle, returns result.

Also contains agent core resolution (plugin lookup).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pluggy

from pynchy.config import get_settings
from pynchy.container_runner._logging import _parse_final_output, _write_run_log
from pynchy.container_runner._mounts import _build_container_args, _build_volume_mounts
from pynchy.container_runner._process import (
    StreamState,
    _graceful_stop,
    read_stderr,
    read_stdout,
)
from pynchy.container_runner._serialization import _input_to_dict
from pynchy.logger import logger
from pynchy.runtime.runtime import get_runtime
from pynchy.types import ContainerInput, ContainerOutput, RegisteredGroup

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

OnProcess = Callable[[asyncio.subprocess.Process, str], Any]
OnOutput = Callable[[ContainerOutput], Awaitable[None]]


# ---------------------------------------------------------------------------
# Agent core resolution
# ---------------------------------------------------------------------------


def resolve_agent_core(plugin_manager: pluggy.PluginManager | None) -> tuple[str, str]:
    """Look up the agent core module and class from plugins.

    Returns (module_path, class_name) for the configured agent core.
    Falls back to the defaults in ContainerInput if no plugin provides one.
    """
    module = "agent_runner.cores.claude"
    class_name = "ClaudeAgentCore"
    if plugin_manager:
        cores = plugin_manager.hook.pynchy_agent_core_info()
        core_info = next((c for c in cores if c["name"] == get_settings().agent.core), None)
        if core_info is None and cores:
            core_info = cores[0]
        if core_info:
            module = core_info["module"]
            class_name = core_info["class_name"]
    return module, class_name


# ---------------------------------------------------------------------------
# Helpers extracted from run_container_agent
# ---------------------------------------------------------------------------


def _collect_plugin_mcp_specs(
    plugin_manager: pluggy.PluginManager,
) -> dict[str, dict] | None:
    """Collect MCP server specs from plugins. Returns None if no specs."""
    plugin_mcp_specs: dict[str, dict] = {}
    mcp_specs_list = plugin_manager.hook.pynchy_mcp_server_spec()
    for spec in mcp_specs_list:
        try:
            plugin_mcp_specs[spec["name"]] = {
                "command": spec["command"],
                "args": spec["args"],
                "env": spec["env"],
            }
        except (KeyError, TypeError):
            logger.exception(
                "Failed to get MCP spec from plugin",
                spec_keys=list(spec.keys()) if isinstance(spec, dict) else str(type(spec)),
            )
    return plugin_mcp_specs or None


def _determine_result(
    state: StreamState,
    exit_code: int | None,
    config_timeout: float,
    container_name: str,
    group_name: str,
    duration_ms: float,
    on_output: OnOutput | None,
    stdout_buf: str,
    stderr_buf: str,
) -> ContainerOutput:
    """Determine final ContainerOutput from run state."""
    if state.timed_out:
        if state.had_streaming_output:
            logger.info(
                "Container timed out after output (idle cleanup)",
                group=group_name,
                container=container_name,
                duration_ms=duration_ms,
            )
            return ContainerOutput(
                status="success", result=None, new_session_id=state.new_session_id
            )

        logger.error(
            "Container timed out with no output",
            group=group_name,
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
            group=group_name,
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
            group=group_name,
            duration_ms=duration_ms,
            new_session_id=state.new_session_id,
        )
        return ContainerOutput(status="success", result=None, new_session_id=state.new_session_id)

    # Legacy mode: parse final output from stdout
    return _parse_final_output(stdout_buf, container_name, stderr_buf, duration_ms)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_container_agent(
    group: RegisteredGroup,
    input_data: ContainerInput,
    on_process: OnProcess,
    on_output: OnOutput | None = None,
    plugin_manager: pluggy.PluginManager | None = None,
) -> ContainerOutput:
    """Low-level primitive — spawn a container agent and stream output.

    **Do not call directly.** Use ``agent_runner.run_agent()`` instead, which
    handles input broadcasting, snapshot writes, session tracking, and output
    routing.  Direct callers bypass the unified message pipeline and the UI
    will not faithfully represent the agent's token stream.

    The only legitimate direct callers are:
    - ``agent_runner.run_agent`` (the unified public entry point)
    - ``plugin_verifier`` (one-off verification containers, not conversations)

    Args:
        group: The registered group configuration.
        input_data: Input payload for the agent-runner.
        on_process: Callback invoked with (proc, container_name) after spawn.
        on_output: If provided, called for each streamed output marker pair.
                   Enables streaming mode. Without it, uses legacy mode.
        plugin_manager: Optional pluggy.PluginManager for plugin MCP mounts and config.

    Returns:
        ContainerOutput with the final status.
    """
    start_time = time.monotonic()
    loop = asyncio.get_running_loop()

    s = get_settings()
    group_dir = s.groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    # --- Resolve worktree ---
    worktree_path: Path | None = None
    if input_data.project_access:
        from pynchy.git_ops.worktree import ensure_worktree

        wt_result = ensure_worktree(group.folder)
        worktree_path = wt_result.path
        if wt_result.notices:
            if input_data.system_notices is None:
                input_data.system_notices = []
            input_data.system_notices.extend(wt_result.notices)

    # --- Build mounts ---
    mounts = _build_volume_mounts(
        group, input_data.is_god, plugin_manager, input_data.project_access, worktree_path
    )

    # --- Collect plugin MCP specs ---
    if plugin_manager and input_data.plugin_mcp_servers is None:
        input_data.plugin_mcp_servers = _collect_plugin_mcp_specs(plugin_manager)

    # --- Container name and args ---
    safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in group.folder)
    container_name = f"pynchy-{safe_name}-{int(time.time() * 1000)}"
    container_args = _build_container_args(mounts, container_name)

    logger.info(
        "Spawning container agent",
        group=group.name,
        container=container_name,
        mount_count=len(mounts),
        is_god=input_data.is_god,
    )

    logs_dir = s.groups_dir / group.folder / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # --- Spawn process ---
    try:
        proc = await asyncio.create_subprocess_exec(
            get_runtime().cli,
            *container_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.error("Failed to spawn container", error=str(exc), container=container_name)
        return ContainerOutput(status="error", result=None, error=f"Spawn failed: {exc}")

    on_process(proc, container_name)

    # Write input JSON and close stdin (Apple Container needs EOF to flush pipe)
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(_input_to_dict(input_data)).encode())
    proc.stdin.close()

    # --- State and timeout ---
    state = StreamState()

    config_timeout = (
        group.container_config.timeout
        if group.container_config and group.container_config.timeout
        else s.container_timeout
    )
    # Grace period: hard timeout must be at least idle_timeout + 30s
    timeout_secs = max(config_timeout, s.idle_timeout + 30.0)
    timeout_handle: asyncio.TimerHandle | None = None

    def kill_on_timeout() -> None:
        state.timed_out = True
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

    # --- Run I/O readers concurrently, then wait for process exit ---
    assert proc.stdout is not None
    assert proc.stderr is not None
    await asyncio.gather(
        read_stdout(
            proc.stdout, state, s.container.max_output_size, group.name, on_output, reset_timeout
        ),
        read_stderr(proc.stderr, state, s.container.max_output_size, group.name),
    )
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
        stdout=state.stdout_buf,
        stderr=state.stderr_buf,
        stdout_truncated=state.stdout_truncated,
        stderr_truncated=state.stderr_truncated,
        duration_ms=duration_ms,
        exit_code=exit_code,
        timed_out=state.timed_out,
        had_streaming_output=state.had_streaming_output,
    )

    # --- Determine result ---
    return _determine_result(
        state=state,
        exit_code=exit_code,
        config_timeout=config_timeout,
        container_name=container_name,
        group_name=group.name,
        duration_ms=duration_ms,
        on_output=on_output,
        stdout_buf=state.stdout_buf,
        stderr_buf=state.stderr_buf,
    )
