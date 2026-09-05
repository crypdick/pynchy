"""Public file-IPC behavior for agent-runner input and output envelopes."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner import ipc
from agent_runner.models import ContainerInput, ContainerOutput


def _input() -> dict[str, object]:
    return {
        "messages": [],
        "group_folder": "group",
        "chat_jid": "chat",
        "is_admin": False,
    }


def test_write_output_serializes_an_atomic_event_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ipc, "IPC_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(ipc.time, "monotonic_ns", lambda: 123)

    ipc.write_output(ContainerOutput(status="success", result="done"))

    output_file = tmp_path / "output" / "123.json"
    assert json.loads(output_file.read_text()) == {
        "status": "success",
        "type": "result",
        "result": "done",
    }
    assert not list((tmp_path / "output").glob("*.tmp"))


def test_read_initial_input_consumes_the_initial_envelope(tmp_path: Path, monkeypatch) -> None:
    initial = tmp_path / "initial.json"
    initial.write_text(json.dumps(_input()))
    monkeypatch.setattr(ipc, "INITIAL_INPUT_FILE", initial)

    result = ipc.read_initial_input()

    assert isinstance(result, ContainerInput)
    assert result.group_folder == "group"
    assert not initial.exists()


def test_drain_ipc_messages_skips_invalid_text_types(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "invalid.json").write_text(json.dumps({"type": "message", "text": 7}))
    monkeypatch.setattr(ipc, "IPC_INPUT_DIR", tmp_path)

    assert ipc.drain_ipc_messages() == []
    assert not (tmp_path / "invalid.json").exists()


def test_drain_ipc_messages_reports_an_unreadable_input_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    input_path = tmp_path / "not-a-directory"
    input_path.write_text("file")
    monkeypatch.setattr(ipc, "IPC_INPUT_DIR", input_path)

    assert ipc.drain_ipc_messages() == []
    assert "IPC drain error" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_wait_for_ipc_followup_returns_none_when_watchdog_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    class _UnavailableObserver:
        daemon = False

        def schedule(self, *_args: object, **_kwargs: object) -> None:
            return None

        def start(self) -> None:
            raise OSError("watch unavailable")

    monkeypatch.setattr(ipc, "IPC_INPUT_DIR", tmp_path)
    monkeypatch.setattr(ipc, "IPC_INPUT_CLOSE_SENTINEL", tmp_path / "_close")
    (tmp_path / "_close").touch()
    monkeypatch.setattr(ipc, "Observer", _UnavailableObserver)

    assert await ipc.wait_for_ipc_followup() is None


@pytest.mark.asyncio
async def test_wait_for_ipc_followup_combines_pending_messages_and_stops_observer(
    tmp_path: Path, monkeypatch
) -> None:
    observer = Mock()
    (tmp_path / "001.json").write_text(
        json.dumps({"type": "message", "text": "first", "turn_id": "turn-1"})
    )
    (tmp_path / "002.json").write_text(
        json.dumps(
            {
                "type": "message",
                "text": "second",
                "query_id": "query-2",
                "metadata": {"source": "warm"},
            }
        )
    )
    monkeypatch.setattr(ipc, "IPC_INPUT_DIR", tmp_path)
    monkeypatch.setattr(ipc, "IPC_INPUT_CLOSE_SENTINEL", tmp_path / "_close")
    monkeypatch.setattr(ipc, "Observer", lambda: observer)

    followup = await ipc.wait_for_ipc_followup()

    assert followup is not None
    assert followup.text == "first\nsecond"
    observer.stop.assert_called_once()
    observer.join.assert_called_once_with(timeout=2)


@pytest.mark.asyncio
async def test_wait_for_ipc_followup_polls_after_a_missed_watchdog_event(
    tmp_path: Path, monkeypatch
) -> None:
    observer = Mock()
    monkeypatch.setattr(ipc, "IPC_INPUT_DIR", tmp_path)
    monkeypatch.setattr(ipc, "IPC_INPUT_CLOSE_SENTINEL", tmp_path / "_close")
    monkeypatch.setattr(ipc, "Observer", lambda: observer)

    async def write_after_timeout() -> None:
        await asyncio.sleep(0.25)
        (tmp_path / "001.json").write_text(json.dumps({"type": "message", "text": "polled"}))

    writer = asyncio.create_task(write_after_timeout())
    followup = await asyncio.wait_for(ipc.wait_for_ipc_followup(), timeout=2)

    assert followup is not None
    assert followup.text == "polled"
    await writer
