"""One-shot direct host runner for agent cores.

This module is launched by the Pynchy host process. It uses the same Pynchy
MCP tools as container sessions, backed by a group-scoped host IPC directory.
Stdin carries one input envelope, stdout streams ``ContainerOutput`` JSON
lines, and stderr is reserved for runner/core logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.parse import urlparse, urlunparse

from agent_runner.core import AgentCore, AgentCoreConfig, AgentEvent
from agent_runner.main import _direct_mcp_server_entry, build_agent_prompt, event_to_output
from agent_runner.models import ContainerInput, ContainerOutput
from agent_runner.registry import create_agent_core


def build_host_core_config(container_input: ContainerInput, *, cwd: str) -> AgentCoreConfig:
    """Build core config for direct host execution."""
    extra = dict(container_input.agent_core_config or {})
    mcp_servers: dict[str, dict[str, object]] = {"pynchy": _host_pynchy_mcp_server(container_input)}
    for server in container_input.mcp_direct_servers or []:
        name = server.get("name")
        if isinstance(name, str) and name:
            mcp_servers[name] = _host_direct_mcp_server_entry(server)

    return AgentCoreConfig(
        cwd=cwd,
        session_id=container_input.session_id,
        group_folder=container_input.group_folder,
        chat_jid=container_input.chat_jid,
        is_admin=container_input.is_admin,
        is_scheduled_task=container_input.is_scheduled_task,
        system_prompt_append=container_input.system_prompt_append,
        mcp_servers=mcp_servers,
        plugin_hooks=container_input.plugin_hooks,
        extra=extra,
    )


def _host_direct_mcp_server_entry(server: dict[str, object]) -> dict[str, object]:
    """Build a local MCP-proxy entry for an agent executing on the host.

    The shared direct-server resolver deliberately uses the container-reachable
    address. Host-direct agents run beside the MCP proxy, so route the same
    proxy URL through localhost instead of crossing the container bridge.
    """
    url = server.get("url")
    if not isinstance(url, str):
        raise TypeError("direct MCP server URL must be a string")
    parsed = urlparse(url)
    if parsed.hostname is None:
        raise ValueError("direct MCP server URL must include a hostname")
    netloc = "localhost"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    local_server = {**server, "url": urlunparse(parsed._replace(netloc=netloc))}
    return _direct_mcp_server_entry(local_server)


def _host_pynchy_mcp_server(container_input: ContainerInput) -> dict[str, object]:
    """Build a direct-host Pynchy MCP entry backed by the host IPC directory."""
    env = {
        "PYNCHY_CHAT_JID": container_input.chat_jid,
        "PYNCHY_GROUP_FOLDER": container_input.group_folder,
        "PYNCHY_IS_ADMIN": "1" if container_input.is_admin else "0",
        "PYNCHY_SESSION_ID": container_input.session_id or "",
        "PYNCHY_IS_SCHEDULED_TASK": "1" if container_input.is_scheduled_task else "0",
    }
    for name in ("PYNCHY_IPC_DIR", "PYNCHY_SKILLS_ROOT"):
        if value := os.environ.get(name):
            env[name] = value
    return {
        "command": sys.executable,
        "args": ["-m", "agent_runner.agent_tools"],
        "env": env,
    }


def _write_output(output: ContainerOutput) -> None:
    sys.stdout.write(json.dumps(output.to_dict()) + "\n")
    sys.stdout.flush()


def _write_error(error: str, session_id: str | None = None) -> None:
    _write_output(ContainerOutput(status="error", new_session_id=session_id, error=error))


def _event_session_id(event: AgentEvent, fallback: str | None) -> str | None:
    if event.type == "system":
        session_id = (event.data.get("system_data") or {}).get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return fallback


async def _run_query(core: AgentCore, prompt: str, session_id: str | None) -> str | None:
    current_session_id = session_id
    async for event in core.query(prompt):
        current_session_id = _event_session_id(event, current_session_id)
        if core.session_id:
            current_session_id = core.session_id
        _write_output(event_to_output(event, current_session_id))
    return core.session_id or current_session_id


def _read_envelope() -> tuple[ContainerInput, str]:
    raw = sys.stdin.read()
    envelope = json.loads(raw)
    if not isinstance(envelope, dict):
        raise TypeError("host runner input must be a JSON object")
    raw_input = envelope.get("input")
    cwd = envelope.get("cwd")
    if not isinstance(raw_input, dict):
        raise TypeError("host runner input.input must be a JSON object")
    if not isinstance(cwd, str) or not cwd:
        raise TypeError("host runner input.cwd must be a non-empty string")
    return ContainerInput.from_dict(raw_input), cwd


async def _main_async() -> int:
    try:
        container_input, cwd = _read_envelope()
        core = create_agent_core(
            container_input.agent_core_module,
            container_input.agent_core_class,
            build_host_core_config(container_input, cwd=cwd),
        )
    except Exception as exc:  # noqa: BLE001, RUF100 - report startup failures to host.  # allow: exception-handling
        _write_error(f"Failed to start host runner: {exc}")
        return 1

    session_id = container_input.session_id
    try:
        await core.start()
        session_id = await _run_query(core, build_agent_prompt(container_input), session_id)
    except Exception as exc:  # noqa: BLE001, RUF100 - report agent failures to host.  # allow: exception-handling
        _write_error(str(exc), session_id)
        return 1
    finally:
        try:
            await core.stop()
        except Exception as exc:  # noqa: BLE001, RUF100 - report cleanup failure on stderr.  # allow: exception-handling
            sys.stderr.write(f"[host-direct] error stopping core: {exc}\n")
            sys.stderr.flush()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
