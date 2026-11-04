"""防止项目分析/实现因过早丢失原文而陷入重复读取。"""

import json
import shlex
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from mini_harness.agent import AgentRunner, AgentRunSpec, ContextConfig
from mini_harness.agent.compaction import ContextGovernor
from mini_harness.providers import LLMResponse, MockProvider, ToolCallRequest
from mini_harness.tools import EditFileTool, FileInfoTool, FindFilesTool, ReadFileTool, RunCommandTool, ToolRegistry, WriteFileTool
from mini_harness.workspace import WorkspaceResolver


def call(name: str, arguments: dict, identifier: str) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCallRequest(id=identifier, name=name, arguments=arguments)])


@pytest.mark.asyncio
async def test_accepted_source_keeps_middle_when_request_fits() -> None:
    """已发送过的源码全文仍是后续编辑的依据，不能只剩文件头。"""
    source = "# comment\n" * 100 + "def crucial_function(): return 42\n" + "# tail\n" * 100
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "implement"}]
    for index in range(4):
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": str(index), "name": "read_file", "arguments": {"path": f"{index}.py"}}
            ]},
            {"role": "tool", "tool_call_id": str(index), "name": "read_file", "content": source},
        ])
    original = deepcopy(messages)
    governor = ContextGovernor(MockProvider([]), ContextConfig(max_context_tokens=16000, max_tool_result_tokens=80))
    governor.accept(len(messages) - 2)
    result = await governor.prepare_request(messages, None, iteration=4, protected_start=1)
    assert result.messages == original
    assert messages == original
    assert not result.accepted_compacted


@pytest.mark.asyncio
async def test_pressure_retains_recent_tool_groups_and_original_history() -> None:
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "implement"}]
    for index in range(8):
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": str(index), "name": "read_file", "arguments": {"path": f"{index}.py"}}
            ]},
            {"role": "tool", "tool_call_id": str(index), "name": "read_file", "content": f"code-{index}\n" + "x" * 2000},
        ])
    original = deepcopy(messages)
    governor = ContextGovernor(MockProvider([]), ContextConfig(max_context_tokens=3500, reserve_output_tokens=300, max_tool_result_tokens=256, enable_auto_compact=False))
    governor.accept(len(messages) - 2)
    result = await governor.prepare_request(messages, None, iteration=8, protected_start=1)
    assert result.accepted_compacted
    assert result.estimated_tokens <= result.available_input_tokens
    assert {"5", "6", "7"} <= {m.get("tool_call_id") for m in result.messages if m.get("role") == "tool"}
    assert messages == original


@pytest.mark.asyncio
async def test_repeat_read_and_budget_hints_are_transient(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    registry = ToolRegistry()
    registry.register(ReadFileTool(WorkspaceResolver().resolve(tmp_path)))
    provider = MockProvider([
        call("read_file", {"path": "app.py"}, "r1"),
        call("read_file", {"path": "app.py"}, "r2"),
        LLMResponse(content="分析完成"),
    ])
    result = await AgentRunner(provider, registry).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "分析"}], max_iterations=3,
    ))
    sent = provider.received_messages[-1]
    assert "重复读取" in sent[0]["content"]
    assert "最多还可进行 1 轮" in sent[0]["content"]
    assert all(message["role"] != "system" for message in result.new_messages)
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_find_files_contributes_to_readonly_progress(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(FindFilesTool(WorkspaceResolver().resolve(tmp_path)))
    provider = MockProvider([
        *(call("find_files", {"pattern": "*.py"}, f"find-{i}") for i in range(6)),
        LLMResponse(content="完成"),
    ])
    result = await AgentRunner(provider, registry).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "分析"}], max_iterations=10,
    ))
    assert result.stop_reason == "completed"
    prompt = provider.received_messages[-1][0]["content"]
    assert "连续执行 6 次只读工具" in prompt
    assert "重复读取" in prompt


@pytest.mark.asyncio
async def test_edit_then_execute_real_verification(tmp_path: Path) -> None:
    """模型从保留的工具正文提取旧值，编辑后通过真正的子进程验证。"""
    target = tmp_path / "app.py"
    target.write_text("# header\n" * 50 + "VALUE = 1\n" + "# tail\n" * 50)
    (tmp_path / "README.md").write_text("demo")
    workspace = WorkspaceResolver().resolve(tmp_path)
    registry = ToolRegistry()
    for tool in (ReadFileTool(workspace), EditFileTool(workspace), RunCommandTool(workspace), WriteFileTool(workspace), FileInfoTool(workspace)):
        registry.register(tool)

    def edit_using_read_content(messages: list[dict]) -> LLMResponse:
        content = next(m["content"] for m in messages if m.get("tool_call_id") == "source")
        assert "VALUE = 1" in content
        return call("edit_file", {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}, "edit")

    command = shlex.join([sys.executable, "-c", "import app; assert app.VALUE == 2; print('verified')"])
    provider = MockProvider([
        call("read_file", {"path": "app.py"}, "source"),
        call("read_file", {"path": "README.md"}, "readme"),
        edit_using_read_content,
        call("write_file", {"path": "notes.txt", "content": "done\n", "mode": "append"}, "append"),
        call("file_info", {"path": "app.py"}, "info"),
        call("run_command", {"command": command}, "verify"),
        MockProvider.final_from_tool_result(),
    ])
    result = await AgentRunner(provider, registry).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "实现并验证"}], max_iterations=8,
    ))
    assert result.stop_reason == "completed"
    assert "verified" in result.final_content and "Exit code: 0" in result.final_content
    assert "VALUE = 2" in target.read_text()
    assert (tmp_path / "notes.txt").read_text() == "done\n"
    info = json.loads(next(m["content"] for m in result.messages if m.get("tool_call_id") == "info"))
    assert info["line_count"] == 101
