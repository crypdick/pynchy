"""Container runner — spawns agent execution in Apple Container.

Spawns subprocesses, writes JSON input to stdin,
parses streaming output using sentinel markers, manages activity-based timeouts,
and writes log files.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pluggy

from pynchy.config import Settings, get_settings
from pynchy.logger import logger
from pynchy.mount_security import validate_additional_mounts
from pynchy.runtime import get_runtime
from pynchy.types import ContainerInput, ContainerOutput, RegisteredGroup, VolumeMount

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
# Serialization helpers (camelCase ↔ snake_case boundary)
# ---------------------------------------------------------------------------


def _input_to_dict(input_data: ContainerInput) -> dict[str, Any]:
    """Convert ContainerInput to dict for the Python agent-runner."""
    d: dict[str, Any] = {
        "messages": input_data.messages,
        "group_folder": input_data.group_folder,
        "chat_jid": input_data.chat_jid,
        "is_god": input_data.is_god,
    }
    if input_data.session_id is not None:
        d["session_id"] = input_data.session_id
    if input_data.is_scheduled_task:
        d["is_scheduled_task"] = True
    if input_data.plugin_mcp_servers is not None:
        d["plugin_mcp_servers"] = input_data.plugin_mcp_servers
    if input_data.system_notices:
        d["system_notices"] = input_data.system_notices
    if input_data.project_access:
        d["project_access"] = True
    # Always include agent core fields (container needs them to import the core)
    d["agent_core_module"] = input_data.agent_core_module
    d["agent_core_class"] = input_data.agent_core_class
    if input_data.agent_core_config is not None:
        d["agent_core_config"] = input_data.agent_core_config
    return d


def _parse_container_output(json_str: str) -> ContainerOutput:
    """Parse JSON from the Python agent-runner into ContainerOutput."""
    data = json.loads(json_str)
    return ContainerOutput(
        status=data["status"],
        result=data.get("result"),
        new_session_id=data.get("new_session_id"),
        error=data.get("error"),
        type=data.get("type", "result"),
        thinking=data.get("thinking"),
        tool_name=data.get("tool_name"),
        tool_input=data.get("tool_input"),
        text=data.get("text"),
        system_subtype=data.get("system_subtype"),
        system_data=data.get("system_data"),
        tool_result_id=data.get("tool_result_id"),
        tool_result_content=data.get("tool_result_content"),
        tool_result_is_error=data.get("tool_result_is_error"),
        result_metadata=data.get("result_metadata"),
    )


# ---------------------------------------------------------------------------
# File preparation helpers
# ---------------------------------------------------------------------------


def _sync_skills(session_dir: Path, plugin_manager: pluggy.PluginManager | None = None) -> None:
    """Copy container/skills/ and plugin skills into the session's .claude/skills/ directory.

    Args:
        session_dir: Path to the .claude directory for this session
        plugin_manager: Optional pluggy.PluginManager for plugin skills
    """
    s = get_settings()
    skills_dst = session_dir / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)

    # Copy built-in skills
    skills_src = s.project_root / "container" / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            dst_dir = skills_dst / skill_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            for f in skill_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, dst_dir / f.name)

    # Copy plugin skills
    if plugin_manager:
        # Hook returns list of lists (one list per plugin)
        skill_path_lists = plugin_manager.hook.pynchy_skill_paths()
        for skill_paths in skill_path_lists:
            try:
                for skill_path_str in skill_paths:
                    skill_path = Path(skill_path_str)
                    if not skill_path.exists() or not skill_path.is_dir():
                        logger.warning(
                            "Plugin skill path does not exist or is not a directory",
                            path=str(skill_path),
                        )
                        continue

                    dst_dir = skills_dst / skill_path.name
                    if dst_dir.exists():
                        raise ValueError(
                            f"Skill name collision: skill '{skill_path.name}' conflicts with "
                            f"an existing skill. Rename the plugin skill directory to "
                            f"avoid shadowing built-in or other plugin skills."
                        )

                    shutil.copytree(skill_path, dst_dir)
                    logger.info(
                        "Synced plugin skill",
                        skill=skill_path.name,
                    )
            except ValueError:
                raise  # Re-raise name collisions — these must not be silenced
            except (OSError, TypeError):
                logger.exception("Failed to sync plugin skills")


def _read_oauth_token() -> str | None:
    """Read the OAuth access token from Claude Code's credentials.

    Checks (in order):
    1. Legacy ~/.claude/.credentials.json file
    2. macOS keychain (service "Claude Code-credentials")
    """
    # 1. Legacy JSON file
    creds_file = Path.home() / ".claude" / ".credentials.json"
    if creds_file.exists():
        try:
            data = json.loads(creds_file.read_text())
            token = data.get("claudeAiOauth", {}).get("accessToken")
            if token:
                return token
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Failed to read legacy credentials file", err=str(exc))

    # 2. macOS keychain
    return _read_oauth_from_keychain()


def _read_oauth_from_keychain() -> str | None:
    """Read OAuth token from the macOS keychain."""
    import subprocess

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _read_gh_token() -> str | None:
    """Read GitHub token from the host's gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed to read GitHub token from gh CLI", err=str(exc))
    return None


def _read_git_identity() -> tuple[str | None, str | None]:
    """Read git user.name and user.email from the host's git config."""
    name = email = None
    for key in ("user.name", "user.email"):
        try:
            r = subprocess.run(
                ["git", "config", key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                if key == "user.name":
                    name = r.stdout.strip()
                else:
                    email = r.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("Failed to read git config", key=key, err=str(exc))
    return name, email


def _shell_quote(value: str) -> str:
    """Quote a value for safe inclusion in a shell env file."""
    return "'" + value.replace("'", "'\\''") + "'"


def _write_env_file() -> Path | None:
    """Write credential env vars for the container. Returns env dir or None.

    Auto-discovers and writes (each independently):
    - Claude credentials: .env file → OAuth token from Claude Code
    - GH_TOKEN: .env file → ``gh auth token``
    - Git identity: ``git config user.name/email`` → GIT_AUTHOR_NAME, etc.

    # TODO: security hardening — generate per-container scoped tokens (GitHub App
    # installation tokens or fine-grained PATs) instead of forwarding the host's
    # full gh token. Each container should have least-privilege credentials scoped
    # to only the repos/permissions it needs.
    """
    s = get_settings()
    env_dir = s.data_dir / "env"
    env_dir.mkdir(parents=True, exist_ok=True)

    env_vars: dict[str, str] = {}

    # --- Read secrets from Settings ---
    secret_map = {
        "ANTHROPIC_API_KEY": s.secrets.anthropic_api_key,
        "OPENAI_API_KEY": s.secrets.openai_api_key,
        "GH_TOKEN": s.secrets.gh_token,
        "CLAUDE_CODE_OAUTH_TOKEN": s.secrets.claude_code_oauth_token,
    }
    for env_name, secret_val in secret_map.items():
        if secret_val is not None:
            env_vars[env_name] = secret_val.get_secret_value()

    # --- Auto-discover Claude credentials ---
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env_vars and "ANTHROPIC_API_KEY" not in env_vars:
        token = _read_oauth_token()
        if token:
            env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = token
            logger.debug("Using OAuth token from Claude Code credentials")

    # --- Auto-discover GH_TOKEN ---
    if "GH_TOKEN" not in env_vars:
        gh_token = _read_gh_token()
        if gh_token:
            env_vars["GH_TOKEN"] = gh_token
            logger.debug("Using GitHub token from gh CLI")

    # --- Auto-discover git identity ---
    git_name, git_email = _read_git_identity()
    if git_name:
        env_vars["GIT_AUTHOR_NAME"] = git_name
        env_vars["GIT_COMMITTER_NAME"] = git_name
    if git_email:
        env_vars["GIT_AUTHOR_EMAIL"] = git_email
        env_vars["GIT_COMMITTER_EMAIL"] = git_email

    if not env_vars:
        logger.warning(
            "No credentials found — containers will fail to authenticate. "
            "Run 'claude' to authenticate or set [secrets].anthropic_api_key in config.toml"
        )
        return None

    logger.debug("Container env prepared", vars=list(env_vars.keys()))
    lines = [f"{k}={_shell_quote(v)}" for k, v in env_vars.items()]
    (env_dir / "env").write_text("\n".join(lines) + "\n")
    return env_dir


def _write_settings_json(session_dir: Path) -> None:
    """Write Claude Code settings.json, merging hook config from scripts/.

    Always regenerates to pick up hook config changes (e.g. guard_git).
    """
    settings_file = session_dir / "settings.json"
    settings: dict[str, Any] = {
        "env": {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
        },
    }

    # Merge hook config from container/scripts/settings.json
    hook_settings_file = get_settings().project_root / "container" / "scripts" / "settings.json"
    if hook_settings_file.exists():
        try:
            hook_settings = json.loads(hook_settings_file.read_text())
            if "hooks" in hook_settings:
                settings["hooks"] = hook_settings["hooks"]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to merge hook settings", err=str(exc))

    settings_file.write_text(json.dumps(settings, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Mount building
# ---------------------------------------------------------------------------


def _build_volume_mounts(
    group: RegisteredGroup,
    is_god: bool,
    plugin_manager: pluggy.PluginManager | None = None,
    project_access: bool = False,
    worktree_path: Path | None = None,
) -> list[VolumeMount]:
    """Build the mount list for a container invocation.

    Args:
        group: The registered group configuration
        is_god: Whether this is the god group
        plugin_manager: Optional pluggy.PluginManager for plugin MCP mounts
        project_access: Whether to mount the host project into the container
        worktree_path: Pre-resolved worktree path for non-main project_access groups

    Returns:
        List of volume mounts for the container
    """
    s = get_settings()
    mounts: list[VolumeMount] = []

    group_dir = s.groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    if worktree_path:
        mounts.append(VolumeMount(str(worktree_path), "/workspace/project", readonly=False))
        # Worktree .git file references the main repo's .git dir via absolute path.
        # Mount it at the same host path so git resolves the reference inside the container.
        git_dir = s.project_root / ".git"
        mounts.append(VolumeMount(str(git_dir), str(git_dir), readonly=False))
        mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))
    else:
        mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))
        global_dir = s.groups_dir / "global"
        if global_dir.exists():
            mounts.append(VolumeMount(str(global_dir), "/workspace/global", readonly=True))

    # Per-group Claude sessions directory (isolated from other groups)
    session_dir = s.data_dir / "sessions" / group.folder / ".claude"
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_settings_json(session_dir)
    _sync_skills(session_dir, plugin_manager)
    mounts.append(VolumeMount(str(session_dir), "/home/agent/.claude", readonly=False))

    # Per-group IPC namespace
    group_ipc_dir = s.data_dir / "ipc" / group.folder
    for sub in ("messages", "tasks", "input", "merge_results"):
        (group_ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append(VolumeMount(str(group_ipc_dir), "/workspace/ipc", readonly=False))

    # Guard scripts (read-only: hook script + settings overlay)
    scripts_dir = s.project_root / "container" / "scripts"
    if scripts_dir.exists():
        mounts.append(VolumeMount(str(scripts_dir), "/workspace/scripts", readonly=True))

    # Environment file directory
    env_dir = _write_env_file()
    if env_dir is not None:
        mounts.append(VolumeMount(str(env_dir), "/workspace/env-dir", readonly=True))

    # Agent-runner source (read-only, Python source for container)
    agent_runner_src = s.project_root / "container" / "agent_runner" / "src"
    mounts.append(VolumeMount(str(agent_runner_src), "/app/src", readonly=True))

    # Additional mounts validated against external allowlist
    if group.container_config and group.container_config.additional_mounts:
        validated = validate_additional_mounts(
            group.container_config.additional_mounts, group.name, is_god
        )
        for m in validated:
            mounts.append(
                VolumeMount(
                    host_path=str(m["hostPath"]),
                    container_path=str(m["containerPath"]),
                    readonly=bool(m["readonly"]),
                )
            )

    # Plugin MCP server source mounts
    if plugin_manager:
        mcp_specs_list = plugin_manager.hook.pynchy_mcp_server_spec()
        for spec in mcp_specs_list:
            try:
                if spec.get("host_source"):
                    # Mount plugin source to /workspace/plugins/{name}/
                    mounts.append(
                        VolumeMount(
                            host_path=str(spec["host_source"]),
                            container_path=f"/workspace/plugins/{spec['name']}",
                            readonly=True,
                        )
                    )
            except Exception:
                logger.exception(
                    "Failed to mount plugin MCP source",
                    plugin_name=spec.get("name", "unknown"),
                    host_source=str(spec.get("host_source", "")),
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
    args.append(get_settings().container.image)
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
    except Exception as exc:
        logger.exception(
            "Graceful stop failed, force killing",
            container=container_name,
            error=str(exc),
        )
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
        f"IsMain: {input_data.is_god}",
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


# ---------------------------------------------------------------------------
# Legacy output parsing (no on_output callback)
# ---------------------------------------------------------------------------


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

OnProcess = Callable[[asyncio.subprocess.Process, str], Any]
OnOutput = Callable[[ContainerOutput], Awaitable[None]]


async def run_container_agent(
    group: RegisteredGroup,
    input_data: ContainerInput,
    on_process: OnProcess,
    on_output: OnOutput | None = None,
    plugin_manager: pluggy.PluginManager | None = None,
) -> ContainerOutput:
    """Spawn a container agent, stream output, manage timeouts, and return result.

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

    # Resolve worktree for all project_access groups (including god).
    # Uses best-effort sync — uncommitted changes from killed containers are
    # preserved and reported via system notices so the agent can resume.
    worktree_path: Path | None = None
    if input_data.project_access:
        from pynchy.worktree import ensure_worktree

        wt_result = ensure_worktree(group.folder)
        worktree_path = wt_result.path
        if wt_result.notices:
            if input_data.system_notices is None:
                input_data.system_notices = []
            input_data.system_notices.extend(wt_result.notices)

    mounts = _build_volume_mounts(
        group, input_data.is_god, plugin_manager, input_data.project_access, worktree_path
    )

    # Collect plugin MCP server specs
    if plugin_manager and input_data.plugin_mcp_servers is None:
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
        if plugin_mcp_specs:
            input_data.plugin_mcp_servers = plugin_mcp_specs

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
        else s.container_timeout
    )
    # Grace period: hard timeout must be at least idle_timeout + 30s
    timeout_secs = max(config_timeout, s.idle_timeout + 30.0)
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
                remaining = s.container.max_output_size - len(stdout_buf)
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
                    start_idx = parse_buffer.find(Settings.OUTPUT_START_MARKER)
                    if start_idx == -1:
                        break
                    end_idx = parse_buffer.find(Settings.OUTPUT_END_MARKER, start_idx)
                    if end_idx == -1:
                        break  # Incomplete pair, wait for more data

                    json_str = parse_buffer[
                        start_idx + len(Settings.OUTPUT_START_MARKER) : end_idx
                    ].strip()
                    parse_buffer = parse_buffer[end_idx + len(Settings.OUTPUT_END_MARKER) :]

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
                remaining = s.container.max_output_size - len(stderr_buf)
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
    is_god: bool,
    tasks: list[dict[str, Any]],
    host_jobs: list[dict[str, str | None]] | None = None,
) -> None:
    """Write current_tasks.json to the group's IPC directory.

    Merges agent tasks and host jobs into a single list. Each entry has
    a "type" field ("agent" or "host") so the container can distinguish them.
    Host jobs are only visible to the god group.
    """
    group_ipc_dir = get_settings().data_dir / "ipc" / folder
    group_ipc_dir.mkdir(parents=True, exist_ok=True)

    # God sees all tasks, others only see their own
    filtered = tasks if is_god else [t for t in tasks if t.get("groupFolder") == folder]

    # Host jobs are god-only
    if is_god and host_jobs:
        filtered = [*filtered, *host_jobs]

    (group_ipc_dir / "current_tasks.json").write_text(json.dumps(filtered, indent=2))


def write_groups_snapshot(
    folder: str,
    is_god: bool,
    groups: list[dict[str, Any]],
    registered_jids: set[str],
) -> None:
    """Write available_groups.json to the group's IPC directory."""
    group_ipc_dir = get_settings().data_dir / "ipc" / folder
    group_ipc_dir.mkdir(parents=True, exist_ok=True)

    # God sees all groups; others see nothing (they can't activate groups)
    visible = groups if is_god else []
    payload = {
        "groups": visible,
        "lastSync": datetime.now(UTC).isoformat(),
    }
    (group_ipc_dir / "available_groups.json").write_text(json.dumps(payload, indent=2))
