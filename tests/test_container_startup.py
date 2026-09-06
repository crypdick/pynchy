"""Startup cleans before spawning and owns rollback until registration."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.agent_protocol.api import ContainerInput
from pynchy.host.container_manager.security.gate import (
    create_gate,
    get_gate_for_group,
    resolve_security,
)
from pynchy.host.container_manager.session import destroy_session, get_session, start_session
from tests.container_runner_support import TEST_GROUP, FakeProcess, _agent_runtime


@pytest.fixture
def startup(tmp_path):
    runtime = replace(_agent_runtime(make_settings()), data_dir=tmp_path, idle_timeout=0.0)
    input_data = ContainerInput(
        messages=[], group_folder=TEST_GROUP.folder, chat_jid=TEST_GROUP.jid, is_admin=False
    )
    return input_data, runtime


async def test_ipc_cleanup_failure_prevents_spawning(startup):
    input_data, runtime = startup
    with (
        patch(
            "pynchy.host.container_manager.session.clean_ipc_input_dir", side_effect=OSError("busy")
        ),
        patch(
            "pynchy.host.container_manager.session._spawn_container", new_callable=AsyncMock
        ) as spawn,
        patch("pynchy.host.container_manager.session.docker_rm_force", new_callable=AsyncMock),
    ):
        with pytest.raises(OSError, match="busy"):
            await start_session(TEST_GROUP, input_data, runtime)
        spawn.assert_not_awaited()
        assert get_session(TEST_GROUP.folder) is None


@pytest.mark.parametrize("error", [OSError("spawn failed"), asyncio.CancelledError("cancelled")])
async def test_failed_startup_retires_its_security_gate(startup, error):
    input_data, runtime = startup

    def spawn(*_args):
        input_data.invocation_ts = 42.0
        create_gate(TEST_GROUP.folder, 42.0, resolve_security(TEST_GROUP.folder))
        raise error

    with (
        patch("pynchy.host.container_manager.session._spawn_container", side_effect=spawn),
        patch("pynchy.host.container_manager.session.docker_rm_force", new_callable=AsyncMock),
        patch("pynchy.host.container_manager.session.clean_secret_files") as clean,
    ):
        with pytest.raises(type(error), match=str(error)):
            await start_session(TEST_GROUP, input_data, runtime)
        assert get_gate_for_group(TEST_GROUP.folder) is None
        assert get_session(TEST_GROUP.folder) is None
        clean.assert_called_once_with(TEST_GROUP.folder)


async def test_first_worker_output_survives_registration(startup):
    input_data, runtime = startup
    output = runtime.data_dir / "ipc" / TEST_GROUP.folder / "output" / "first.json"
    output.parent.mkdir(parents=True)
    stale = output.with_name("stale.json")
    stale.write_text("stale")
    proc = FakeProcess()

    def spawn(_group, _input, name, *_args):
        assert not stale.exists()
        output.write_text('{"type": "text", "text": "hello"}')
        return proc, ()

    with (
        patch("pynchy.host.container_manager.session._spawn_container", side_effect=spawn),
        patch("pynchy.host.container_manager.session.docker_rm_force", new_callable=AsyncMock),
        patch("pynchy.host.container_manager.session.graceful_stop", new_callable=AsyncMock),
    ):
        session, failures = await start_session(TEST_GROUP, input_data, runtime)
        try:
            assert get_session(TEST_GROUP.folder) is session
            assert failures == ()
            assert output.read_text() == '{"type": "text", "text": "hello"}'
        finally:
            await destroy_session(TEST_GROUP.folder)


async def test_failed_process_attachment_stops_the_worker(startup):
    input_data, runtime = startup
    proc = FakeProcess()
    proc.stderr = None
    with (
        patch(
            "pynchy.host.container_manager.session._spawn_container",
            new=AsyncMock(return_value=(proc, ())),
        ),
        patch("pynchy.host.container_manager.session.docker_rm_force", new_callable=AsyncMock),
        patch(
            "pynchy.host.container_manager.session.graceful_stop", new_callable=AsyncMock
        ) as stop,
    ):
        with pytest.raises(RuntimeError, match="without stderr pipe"):
            await start_session(TEST_GROUP, input_data, runtime)
        stop.assert_awaited_once()
        assert stop.await_args.args[0] is proc
        assert get_session(TEST_GROUP.folder) is None
