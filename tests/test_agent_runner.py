"""Tests for src/pynchy/agent/agent_runner/src/agent_runner/main.py.

Tests core functions: build_sdk_messages, event_to_output, ContainerOutput,
ContainerInput, should_close, drain_ipc_input, build_core_config.
"""

from __future__ import annotations

import json

# We need to adjust the import path since agent_runner lives in src/pynchy/agent/
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from typing import TYPE_CHECKING

from agent_runner.events import (
    ResultEvent,
    ResultMetadata,
    SystemEvent,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from agent_runner.ipc import IpcMessage, drain_ipc_input, drain_ipc_messages, should_close
from agent_runner.main import (
    build_agent_prompt,
    build_initial_prompt,
    build_sdk_messages,
    event_to_output,
)
from agent_runner.main import main as run_agent_main
from agent_runner.models import ContainerInput, ContainerOutput

if TYPE_CHECKING:
    from agent_runner.core import AgentCoreConfig

# ---------------------------------------------------------------------------
# ContainerOutput.to_dict
# ---------------------------------------------------------------------------


class TestContainerOutputToDict:
    """Test ContainerOutput serialization for each event type."""

    def test_result_basic(self):
        out = ContainerOutput(status="success", result="hello world")
        d = out.to_dict()
        assert d["type"] == "result"
        assert d["status"] == "success"
        assert d["result"] == "hello world"

    def test_result_with_session_id(self):
        out = ContainerOutput(status="success", result="done", new_session_id="sess-123")
        d = out.to_dict()
        assert d["new_session_id"] == "sess-123"

    def test_result_with_error(self):
        out = ContainerOutput(status="error", error="something broke")
        d = out.to_dict()
        assert d["error"] == "something broke"
        assert d["status"] == "error"

    def test_result_with_metadata(self):
        meta = {"total_cost_usd": 0.05, "duration_ms": 1200}
        out = ContainerOutput(status="success", result="ok", result_metadata=meta)
        d = out.to_dict()
        assert d["result_metadata"] == meta

    def test_result_omits_none_fields(self):
        out = ContainerOutput(status="success", result=None)
        d = out.to_dict()
        assert "new_session_id" not in d
        assert "error" not in d
        assert "result_metadata" not in d

    def test_thinking_type(self):
        out = ContainerOutput(status="success", type="thinking", thinking="let me think...")
        d = out.to_dict()
        assert d["type"] == "thinking"
        assert d["thinking"] == "let me think..."
        assert "result" not in d

    def test_tool_use_type(self):
        out = ContainerOutput(
            status="success",
            type="tool_use",
            tool_name="bash",
            tool_input={"command": "ls"},
        )
        d = out.to_dict()
        assert d["type"] == "tool_use"
        assert d["tool_name"] == "bash"
        assert d["tool_input"] == {"command": "ls"}

    def test_text_type(self):
        out = ContainerOutput(status="success", type="text", text="streaming text")
        d = out.to_dict()
        assert d["type"] == "text"
        assert d["text"] == "streaming text"

    def test_system_type(self):
        out = ContainerOutput(
            status="success",
            type="system",
            system_subtype="init",
            system_data={"session_id": "abc"},
        )
        d = out.to_dict()
        assert d["type"] == "system"
        assert d["system_subtype"] == "init"
        assert d["system_data"] == {"session_id": "abc"}

    def test_tool_result_type(self):
        out = ContainerOutput(
            status="success",
            type="tool_result",
            tool_result_id="tr-1",
            tool_result_content="file created",
            tool_result_is_error=False,
        )
        d = out.to_dict()
        assert d["type"] == "tool_result"
        assert d["tool_result_id"] == "tr-1"
        assert d["tool_result_content"] == "file created"
        assert d["tool_result_is_error"] is False


class TestContainerInput:
    """Test ContainerInput parsing from dict."""

    def test_minimal_input(self):
        data = {
            "messages": [{"content": "hi"}],
            "group_folder": "test",
            "chat_jid": "123@g.us",
            "is_admin": True,
        }
        ci = ContainerInput.from_dict(data)
        assert ci.messages == [{"content": "hi"}]
        assert ci.group_folder == "test"
        assert ci.chat_jid == "123@g.us"
        assert ci.is_admin is True
        assert ci.session_id is None
        assert ci.is_scheduled_task is False
        assert ci.repo_access is None

    def test_full_input(self):
        data = {
            "messages": [],
            "session_id": "sess-1",
            "group_folder": "grp",
            "chat_jid": "456@g.us",
            "is_admin": False,
            "is_scheduled_task": True,
            "system_notices": ["notice1"],
            "repo_access": "owner/pynchy",
            "agent_core_module": "custom.mod",
            "agent_core_class": "CustomCore",
            "agent_core_config": {"model": "gpt-4"},
        }
        ci = ContainerInput.from_dict(data)
        assert ci.session_id == "sess-1"
        assert ci.is_scheduled_task is True
        assert ci.system_notices == ["notice1"]
        assert ci.repo_access == "owner/pynchy"
        assert ci.agent_core_module == "custom.mod"
        assert ci.agent_core_class == "CustomCore"
        assert ci.agent_core_config == {"model": "gpt-4"}

    def test_scheduled_prompt_states_contract_without_choreography(self):
        ci = ContainerInput.from_dict(
            {
                "messages": [
                    {
                        "sender_name": "Scheduler",
                        "content": (
                            "Review the repo.\n\n"
                            "[POST-WORK REFLECTION]\n"
                            "Review for missing reports."
                        ),
                    }
                ],
                "group_folder": "review",
                "chat_jid": "scheduled:review",
                "is_admin": False,
                "is_scheduled_task": True,
            }
        )

        with patch("agent_runner.main.drain_ipc_input", return_value=[]):
            prompt = build_initial_prompt(ci)

        message_prompt = build_sdk_messages(ci.messages)
        assert prompt.endswith(message_prompt)
        assert prompt != message_prompt

    def test_shared_scheduled_prompt_contract_applies_without_container_ipc(self):
        ci = ContainerInput.from_dict(
            {
                "messages": [
                    {
                        "sender_name": "Scheduler",
                        "content": (
                            "Review the repo.\n\n"
                            "[POST-WORK REFLECTION]\n"
                            "Review for missing reports."
                        ),
                    }
                ],
                "group_folder": "review",
                "chat_jid": "scheduled:review",
                "is_admin": False,
                "is_scheduled_task": True,
            }
        )

        prompt = build_agent_prompt(ci)

        assert prompt.endswith(build_sdk_messages(ci.messages))

    def test_post_work_reflection_is_scoped_to_scheduled_tasks(self):
        ci = ContainerInput.from_dict(
            {
                "messages": [{"sender_name": "User", "content": "Review the repo."}],
                "group_folder": "review",
                "chat_jid": "chat:review",
                "is_admin": False,
            }
        )

        prompt = build_agent_prompt(ci)

        assert prompt == build_sdk_messages(ci.messages)

    def test_defaults_agent_core(self):
        data = {
            "messages": [],
            "group_folder": "g",
            "chat_jid": "j",
            "is_admin": False,
        }
        ci = ContainerInput.from_dict(data)
        assert ci.agent_core_module == "agent_runner.cores.openai"
        assert ci.agent_core_class == "OpenAIAgentCore"

    def test_missing_required_field_raises(self):
        with pytest.raises(TypeError):
            ContainerInput.from_dict({"messages": []})  # missing group_folder, chat_jid, is_admin


class TestBuildSdkMessages:
    """Test message list to XML conversion."""

    def test_empty_list(self):
        assert not build_sdk_messages([])

    def test_single_message(self):
        msgs = [
            {
                "sender_name": "Alice",
                "timestamp": "2024-01-01T00:00:00Z",
                "content": "Hello",
            }
        ]
        result = build_sdk_messages(msgs)
        assert "<messages>" in result
        assert "</messages>" in result
        assert 'sender="Alice"' in result
        assert ">Hello</message>" in result

    def test_multiple_messages(self):
        msgs = [
            {"sender_name": "Alice", "timestamp": "t1", "content": "Hi"},
            {"sender_name": "Bob", "timestamp": "t2", "content": "Hey"},
        ]
        result = build_sdk_messages(msgs)
        assert result.count("<message ") == 2
        assert 'sender="Alice"' in result
        assert 'sender="Bob"' in result

    def test_xml_escaping(self):
        msgs = [
            {
                "sender_name": 'Test "User"',
                "timestamp": "t",
                "content": "Use <b>bold</b> & stuff",
            }
        ]
        result = build_sdk_messages(msgs)
        assert "&amp;" in result
        assert "&lt;b&gt;" in result
        assert "&quot;" in result

    def test_missing_fields_use_defaults(self):
        msgs = [{}]
        result = build_sdk_messages(msgs)
        assert 'sender="Unknown"' in result

    def test_ampersand_in_content(self):
        msgs = [{"content": "A & B", "sender_name": "X", "timestamp": "t"}]
        result = build_sdk_messages(msgs)
        assert "A &amp; B" in result

    def test_semantic_context_is_included_without_metadata(self):
        msgs = [
            {
                "sender_name": "Alice",
                "timestamp": "t",
                "content": "",
                "context": {
                    "attachments": [{"filename": "voice.ogg", "content_type": "audio/ogg"}]
                },
                "metadata": {"source": "discord_canary", "synthetic_user_input": True},
            }
        ]

        result = build_sdk_messages(msgs)

        assert "<context>" in result
        assert "<metadata>" not in result
        assert "voice.ogg" in result
        assert "audio/ogg" in result


class TestEventToOutput:
    """Test AgentEvent to ContainerOutput conversion."""

    def test_thinking_event(self):
        event = ThinkingEvent(thinking="hmm")
        out = event_to_output(event, "sess-1")
        assert out.type == "thinking"
        assert out.thinking == "hmm"
        assert out.status == "success"

    def test_tool_use_event(self):
        event = ToolUseEvent(tool_name="bash", tool_input={"command": "ls"})
        out = event_to_output(event, None)
        assert out.type == "tool_use"
        assert out.tool_name == "bash"
        assert out.tool_input == {"command": "ls"}

    def test_tool_result_event(self):
        event = ToolResultEvent(
            tool_result_id="tr-1",
            tool_result_content="ok",
            tool_result_is_error=False,
        )
        out = event_to_output(event, None)
        assert out.type == "tool_result"
        assert out.tool_result_id == "tr-1"
        assert out.tool_result_is_error is False

    def test_text_event(self):
        event = TextEvent(text="hello")
        out = event_to_output(event, None)
        assert out.type == "text"
        assert out.text == "hello"

    def test_system_event(self):
        event = SystemEvent(system_subtype="init", system_data={"session_id": "s1"})
        out = event_to_output(event, None)
        assert out.type == "system"
        assert out.system_subtype == "init"
        assert out.system_data == {"session_id": "s1"}

    def test_result_event_includes_session(self):
        event = ResultEvent(
            result="Final answer",
            result_metadata=ResultMetadata(subtype="result", is_error=False, extra={"cost": 0.01}),
        )
        out = event_to_output(event, "sess-42")
        assert out.type == "result"
        assert out.status == "success"
        assert out.result == "Final answer"
        assert out.new_session_id == "sess-42"
        assert out.result_metadata == {
            "subtype": "result",
            "is_error": False,
            "session_id": None,
            "cost": 0.01,
        }
        assert out.error is None

    def test_result_event_with_is_error(self):
        """SDK is_error=True should produce status='error' with error field set."""
        error_text = 'API Error: 429 {"error":{"type":"rate_limit_error"}}'
        event = ResultEvent(
            result=error_text,
            result_metadata=ResultMetadata(subtype="error", is_error=True, extra={"num_turns": 0}),
        )
        out = event_to_output(event, "sess-99")
        assert out.status == "error"
        assert out.error == error_text
        assert out.result == error_text
        assert out.new_session_id == "sess-99"


class TestCoreStartupErrors:
    """Startup failures retain their query-generation correlation."""

    @staticmethod
    def _input() -> ContainerInput:
        return ContainerInput(
            messages=[],
            group_folder="test-group",
            chat_jid="test@g.us",
            is_admin=False,
            query_id="query-startup",
        )

    @pytest.mark.asyncio
    async def test_create_failure_keeps_query_id(self, tmp_path: Path):
        outputs: list[ContainerOutput] = []
        input_dir = tmp_path / "input"
        with (
            patch("agent_runner.main.read_initial_input", return_value=self._input()),
            patch("agent_runner.main.build_initial_prompt", return_value="prompt"),
            patch("agent_runner.main.create_agent_core", side_effect=RuntimeError("bad core")),
            patch("agent_runner.main.write_output", side_effect=outputs.append),
            patch("agent_runner.main.IPC_INPUT_DIR", input_dir),
            patch("agent_runner.main.IPC_INPUT_CLOSE_SENTINEL", input_dir / "_close"),
            pytest.raises(SystemExit),
        ):
            await run_agent_main()

        assert outputs[0].query_id == "query-startup"
        assert outputs[0].error is not None
        assert "Failed to create agent core" in outputs[0].error

    @pytest.mark.asyncio
    async def test_start_failure_keeps_query_id(self, tmp_path: Path):
        outputs: list[ContainerOutput] = []
        input_dir = tmp_path / "input"
        core = AsyncMock()
        core.start.side_effect = RuntimeError("startup failed")
        with (
            patch("agent_runner.main.read_initial_input", return_value=self._input()),
            patch("agent_runner.main.build_initial_prompt", return_value="prompt"),
            patch("agent_runner.main.create_agent_core", return_value=core),
            patch("agent_runner.main.write_output", side_effect=outputs.append),
            patch("agent_runner.main.IPC_INPUT_DIR", input_dir),
            patch("agent_runner.main.IPC_INPUT_CLOSE_SENTINEL", input_dir / "_close"),
            pytest.raises(SystemExit),
        ):
            await run_agent_main()

        assert outputs[0].query_id == "query-startup"
        assert outputs[0].error == "Failed to start agent core: startup failed"

    @pytest.mark.asyncio
    async def test_initial_input_failure_is_reported_to_host(self, monkeypatch):
        outputs: list[ContainerOutput] = []

        def fail_to_read_input():
            raise RuntimeError("invalid input")

        monkeypatch.setattr("agent_runner.main.read_initial_input", fail_to_read_input)
        monkeypatch.setattr("agent_runner.main.write_output", outputs.append)

        with pytest.raises(SystemExit):
            await run_agent_main()

        assert outputs == [
            ContainerOutput(status="error", error="Failed to read initial input: invalid input")
        ]


class TestScheduledReportFollowupContext:
    """Verify a completed scheduled report remains in the provider conversation."""

    @pytest.mark.asyncio
    async def test_human_followup_sees_exact_scheduled_findings(self, tmp_path: Path):
        report = "Alert findings: queue lag is 47 minutes; deployed SHA is abc123."
        human_reply = "Which queue and deployment did that alert identify?"
        created_cores = []
        outputs: list[ContainerOutput] = []

        class TranscriptCore:
            """Minimal provider model whose context lives with one core instance."""

            def __init__(self, config: AgentCoreConfig) -> None:
                self.config = config
                self.history: list[str] = []
                self.contexts: list[str] = []
                self._session_id = ""

            @property
            def session_id(self) -> str:
                return self._session_id

            async def start(self) -> None:
                return None

            async def query(self, prompt: str):
                context = "\n".join((*self.history, f"user: {prompt}"))
                self.contexts.append(context)
                if not self.history:
                    yield SystemEvent(
                        system_subtype="init",
                        system_data={"session_id": "provider-session-scheduled-report"},
                    )
                if not self.history:
                    result = report
                elif report in context:
                    result = "The alert identified queue lag at 47 minutes on SHA abc123."
                else:
                    result = "The scheduled findings are missing."
                self.history.extend((f"user: {prompt}", f"assistant: {result}"))
                yield ResultEvent(
                    result=result,
                    result_metadata=ResultMetadata(subtype="result", is_error=False),
                )

            async def stop(self) -> None:
                return None

        def create_core(
            _module_path: str,
            _class_name: str,
            config: AgentCoreConfig,
        ) -> TranscriptCore:
            core = TranscriptCore(config)
            created_cores.append(core)
            return core

        initial = ContainerInput(
            messages=[
                {
                    "message_type": "user",
                    "sender": "scheduled_task",
                    "sender_name": "Scheduled Task",
                    "content": "Inspect the production alert and report exact findings.",
                }
            ],
            group_folder="scheduled-report",
            chat_jid="discord:thread:scheduled-report",
            is_admin=False,
            is_scheduled_task=True,
            turn_id="turn-scheduled",
            query_id="query-scheduled",
        )
        followup = IpcMessage(
            text=f'<messages><message sender="Operator">{human_reply}</message></messages>',
            turn_id="turn-human",
            query_id="query-human",
        )
        ipc_dir = tmp_path / "input"

        with (
            patch("agent_runner.main.read_initial_input", return_value=initial),
            patch("agent_runner.main.create_agent_core", side_effect=create_core),
            patch(
                "agent_runner.main.wait_for_ipc_followup",
                new_callable=AsyncMock,
                side_effect=[followup, None],
            ),
            patch("agent_runner.main.should_close", return_value=False),
            patch("agent_runner.main.write_output", side_effect=outputs.append),
            patch("agent_runner.main.IPC_INPUT_DIR", ipc_dir),
            patch("agent_runner.main.IPC_INPUT_CLOSE_SENTINEL", ipc_dir / "_close"),
            patch("agent_runner.ipc.IPC_INPUT_DIR", ipc_dir),
        ):
            await run_agent_main()

        assert len(created_cores) == 1
        provider_context = created_cores[0].contexts[1]
        assert human_reply in provider_context
        assert report in provider_context
        assert [output.result for output in outputs if output.result] == [
            report,
            "The alert identified queue lag at 47 minutes on SHA abc123.",
        ]
        assert outputs[-1].new_session_id == "provider-session-scheduled-report"
        assert outputs[-1].query_id == "query-human"


class TestShouldClose:
    """Test _close sentinel detection."""

    def test_no_sentinel(self, tmp_path):
        with patch("agent_runner.ipc.IPC_INPUT_CLOSE_SENTINEL", tmp_path / "_close"):
            assert should_close() is False

    def test_sentinel_exists(self, tmp_path):
        sentinel = tmp_path / "_close"
        sentinel.touch()
        with patch("agent_runner.ipc.IPC_INPUT_CLOSE_SENTINEL", sentinel):
            assert should_close() is True
            # Sentinel should be cleaned up
            assert not sentinel.exists()


class TestDrainIpcInput:
    """Test IPC input message draining."""

    def test_empty_directory(self, tmp_path):
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == []

    def test_single_message(self, tmp_path):
        msg_file = tmp_path / "001.json"
        msg_file.write_text(json.dumps({"type": "message", "text": "hello"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == ["hello"]
            assert not msg_file.exists()  # File should be consumed

    def test_message_envelope_preserves_turn_metadata(self, tmp_path):
        msg_file = tmp_path / "001.json"
        msg_file.write_text(
            json.dumps(
                {
                    "type": "message",
                    "text": "hello",
                    "turn_id": "turn_2",
                    "query_id": "query_2",
                    "metadata": {"pynchy_turn_id": "turn_2", "source": "warm"},
                }
            )
        )
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_messages()

        assert len(result) == 1
        assert result[0].text == "hello"
        assert result[0].turn_id == "turn_2"
        assert result[0].query_id == "query_2"
        assert result[0].metadata == {"pynchy_turn_id": "turn_2", "source": "warm"}

    def test_multiple_messages_sorted(self, tmp_path):
        (tmp_path / "002.json").write_text(json.dumps({"type": "message", "text": "second"}))
        (tmp_path / "001.json").write_text(json.dumps({"type": "message", "text": "first"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == ["first", "second"]

    def test_skips_non_message_types(self, tmp_path):
        (tmp_path / "001.json").write_text(json.dumps({"type": "other", "text": "ignored"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == []

    def test_skips_messages_without_text(self, tmp_path):
        (tmp_path / "001.json").write_text(json.dumps({"type": "message"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == []

    def test_handles_malformed_json(self, tmp_path):
        (tmp_path / "001.json").write_text("not json")
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == []
            assert not (tmp_path / "001.json").exists()  # Bad file cleaned up

    def test_ignores_non_json_files(self, tmp_path):
        (tmp_path / "readme.txt").write_text("not a message")
        (tmp_path / "001.json").write_text(json.dumps({"type": "message", "text": "hi"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == ["hi"]
            assert (tmp_path / "readme.txt").exists()  # Non-JSON untouched
