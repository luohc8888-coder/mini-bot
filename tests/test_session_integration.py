"""AgentLoop + Session + MockProvider 的 V1 集成测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import mini_harness.session.manager as manager_module
from mini_harness.agent import AgentLoop, AgentRunner, build_system_prompt
from mini_harness.bus import InboundMessage, MessageBus
from mini_harness.cli import Console, InteractiveCLI, ProfileStore, ProviderProfile
from mini_harness.providers import LLMResponse, MockProvider, ToolCallRequest
from mini_harness.session import SessionManager
from mini_harness.tools import AddTool, ToolRegistry


class ScriptedConsole(Console):
    """按顺序提供输入并记录输出。"""

    def __init__(self, answers: list[str]) -> None:
        super().__init__(color=False)
        self.answers = iter(answers)
        self.output: list[str] = []

    def write(self, text: str = "") -> None:
        self.output.append(text)

    def write_error(self, text: str) -> None:
        self.output.append(f"错误：{text}")

    def write_tool(self, text: str) -> None:
        self.output.append(f"工具：{text}")

    def write_assistant(self, text: str) -> None:
        self.output.append(f"助手：{text}")

    async def read(self, prompt: str = "") -> str:
        try:
            return next(self.answers)
        except StopIteration as exc:
            raise EOFError from exc


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def registry() -> ToolRegistry:
    result = ToolRegistry()
    result.register(AddTool())
    return result


def first_turn_provider() -> MockProvider:
    return MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_add_001",
                        name="add",
                        arguments={"a": 2, "b": 3},
                    )
                ],
                finish_reason="tool_calls",
            ),
            MockProvider.final_from_tool_result(prefix="结果是"),
        ]
    )


async def run_one_message(bus: MessageBus, content: str) -> Any:
    await bus.publish_inbound(InboundMessage(content=content, sender="cli"))
    return await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)


@pytest.mark.asyncio
async def test_same_workspace_restart_restores_history(tmp_path: Path) -> None:
    """同一 Workspace 重启后，新 SessionManager 能看到旧历史。"""
    workspace = make_workspace(tmp_path)
    data_root = tmp_path / "data"
    manager = SessionManager(workspace, data_root=data_root)
    bus = MessageBus()
    agent_loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(first_turn_provider(), registry()),
        session_manager=manager,
    )
    task = asyncio.create_task(agent_loop.run())
    try:
        outbound = await run_one_message(bus, "计算 2 + 3")
        assert outbound.content == "结果是 5"
    finally:
        agent_loop.stop()
        await task

    restored_manager = SessionManager(workspace, data_root=data_root)
    restored = restored_manager.load()
    assert len(restored.messages) == 4
    assert restored.messages[0] == {"role": "user", "content": "计算 2 + 3"}
    assert restored.messages[-1] == {"role": "assistant", "content": "结果是 5"}


@pytest.mark.asyncio
async def test_different_workspaces_do_not_share_history(tmp_path: Path) -> None:
    """两个 Workspace 使用独立 Session 文件。"""
    data_root = tmp_path / "data"
    first_ws = make_workspace(tmp_path)
    second_ws = tmp_path / "second"
    second_ws.mkdir()
    first_manager = SessionManager(first_ws, data_root=data_root)
    second_manager = SessionManager(second_ws, data_root=data_root)

    bus = MessageBus()
    first_loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(first_turn_provider(), registry()),
        session_manager=first_manager,
    )
    task = asyncio.create_task(first_loop.run())
    try:
        await run_one_message(bus, "计算 2 + 3")
    finally:
        first_loop.stop()
        await task

    assert len(first_manager.load().messages) == 4
    assert second_manager.load().messages == []


@pytest.mark.asyncio
async def test_second_turn_does_not_duplicate_old_history(tmp_path: Path) -> None:
    """连续两轮保存后，第一轮历史只出现一份。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_add_001",
                        name="add",
                        arguments={"a": 2, "b": 3},
                    )
                ],
                finish_reason="tool_calls",
            ),
            MockProvider.final_from_tool_result(prefix="结果是"),
            LLMResponse(content="第二轮回答", finish_reason="stop"),
        ]
    )
    bus = MessageBus()
    agent_loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry()),
        session_manager=manager,
    )
    task = asyncio.create_task(agent_loop.run())
    try:
        first = await run_one_message(bus, "计算 2 + 3")
        second = await run_one_message(bus, "第二轮")
    finally:
        agent_loop.stop()
        await task

    assert first.content == "结果是 5"
    assert second.content == "第二轮回答"
    messages = manager.load().messages
    assert len(messages) == 6
    assert sum(
        1 for message in messages if message.get("content") == "计算 2 + 3"
    ) == 1
    assert sum(
        1 for message in messages if message.get("content") == "结果是 5"
    ) == 1


@pytest.mark.asyncio
async def test_second_turn_provider_receives_full_first_turn_history_after_restart(
    tmp_path: Path,
) -> None:
    """第二轮 Provider 的入参必须精确包含第一轮完整历史。

    这个测试直接检查 ``MockProvider.received_messages``，而不是检查
    CLI 打印了什么。因此如果 AgentLoop 只把历史留在界面、没有传给下一轮
    的 ``Provider.chat``，这里会失败。
    """
    workspace = make_workspace(tmp_path)
    data_root = tmp_path / "data"
    manager = SessionManager(workspace, data_root=data_root)
    first_bus = MessageBus()
    first_loop = AgentLoop(
        bus=first_bus,
        runner=AgentRunner(first_turn_provider(), registry()),
        session_manager=manager,
    )
    task = asyncio.create_task(first_loop.run())
    try:
        first_outbound = await run_one_message(first_bus, "计算 2 + 3")
    finally:
        first_loop.stop()
        await task

    assert first_outbound.content == "结果是 5"
    first_history = manager.load().messages
    assert [message["role"] for message in first_history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert first_history[0] == {"role": "user", "content": "计算 2 + 3"}
    assert first_history[1]["tool_calls"] == [
        {
            "id": "call_add_001",
            "name": "add",
            "arguments": {"a": 2, "b": 3},
        }
    ]
    assert first_history[2] == {
        "role": "tool",
        "tool_call_id": "call_add_001",
        "name": "add",
        "content": "5",
    }
    assert first_history[3] == {"role": "assistant", "content": "结果是 5"}

    # 重新创建 Manager 和 Provider，模拟进程重启。
    restored_manager = SessionManager(workspace, data_root=data_root)
    second_provider = MockProvider(
        [MockProvider.final_from_tool_result(prefix="刚才的结果是")]
    )
    second_bus = MessageBus()
    second_loop = AgentLoop(
        bus=second_bus,
        runner=AgentRunner(second_provider, registry()),
        session_manager=restored_manager,
    )
    second_task = asyncio.create_task(second_loop.run())
    second_user_text = "刚才计算的结果是多少？"
    try:
        outbound = await run_one_message(second_bus, second_user_text)
    finally:
        second_loop.stop()
        await second_task

    assert outbound.content == "刚才的结果是 5"
    assert len(second_provider.received_messages) == 1
    sent = second_provider.received_messages[0]

    # 关键断言：第二轮模型实际收到的输入 = System Prompt + 第一轮完整历史
    # + 当前用户消息；System Prompt 不进入 Session 历史。
    expected = [
        {"role": "system", "content": build_system_prompt(workspace)},
        *first_history,
        {"role": "user", "content": second_user_text},
    ]
    assert sent == expected
    assert [message["role"] for message in sent] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert sent[1]["content"] == "计算 2 + 3"
    assert sent[2]["tool_calls"][0]["id"] == "call_add_001"
    assert sent[3]["tool_call_id"] == "call_add_001"
    assert sent[4]["content"] == "结果是 5"
    assert sent[5] == {"role": "user", "content": second_user_text}
    assert all(message["role"] != "system" for message in manager.load().messages)


@pytest.mark.asyncio
async def test_corrupt_session_is_reported_without_overwrite(tmp_path: Path) -> None:
    """AgentLoop 遇到损坏 Session 时返回明确错误，且不覆盖文件。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    bad = "{broken"
    manager.path.write_text(bad, encoding="utf-8")
    bus = MessageBus()
    agent_loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(MockProvider([LLMResponse(content="不应调用")]), registry()),
        session_manager=manager,
    )
    task = asyncio.create_task(agent_loop.run())
    try:
        outbound = await run_one_message(bus, "你好")
    finally:
        agent_loop.stop()
        await task

    assert outbound.metadata["error"] == "session_load_error"
    assert "Session 加载失败" in outbound.content
    assert manager.path.read_text(encoding="utf-8") == bad


@pytest.mark.asyncio
async def test_save_failure_reports_error_and_keeps_old_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存失败不发送正常完成消息，旧文件保持不变。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    old_text = json.dumps(
        {
            "schema_version": 1,
            "workspace": str(manager.workspace),
            "session_key": "cli:main",
            "messages": [{"role": "user", "content": "旧历史"}],
        },
        ensure_ascii=False,
    )
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(old_text, encoding="utf-8")

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(manager_module.os, "replace", failing_replace)
    bus = MessageBus()
    agent_loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(
            MockProvider([LLMResponse(content="不应发布")]),
            registry(),
        ),
        session_manager=manager,
    )
    task = asyncio.create_task(agent_loop.run())
    try:
        outbound = await run_one_message(bus, "会保存失败")
    finally:
        agent_loop.stop()
        await task

    assert outbound.metadata["error"] == "session_save_error"
    assert "Session 保存失败" in outbound.content
    assert manager.path.read_text(encoding="utf-8") == old_text


@pytest.mark.asyncio
async def test_max_iterations_finalized_turn_is_saved(tmp_path: Path) -> None:
    """预算耗尽但收尾成功时，最终总结必须写入 Session。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_budget",
                        name="add",
                        arguments={"a": 2, "b": 3},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="预算耗尽总结：已经完成加法。", finish_reason="stop"),
        ]
    )
    bus = MessageBus()
    agent_loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry()),
        max_iterations=1,
        session_manager=manager,
    )
    task = asyncio.create_task(agent_loop.run())
    try:
        outbound = await run_one_message(bus, "执行一次工具")
    finally:
        agent_loop.stop()
        await task

    assert outbound.content == "预算耗尽总结：已经完成加法。"
    assert outbound.metadata["stop_reason"] == "max_iterations_finalized"
    saved = manager.load().messages
    assert [message["role"] for message in saved] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert saved[-1] == {
        "role": "assistant",
        "content": "预算耗尽总结：已经完成加法。",
    }
    assert all(message["role"] != "system" for message in saved)


@pytest.mark.asyncio
async def test_incomplete_turn_is_not_saved_to_session(tmp_path: Path) -> None:
    """预算耗尽且收尾失败时，不把失败回合写进 Session。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_incomplete",
                        name="add",
                        arguments={"a": 1, "b": 2},
                    )
                ],
                finish_reason="tool_calls",
            ),
            # finalize_on_max_iterations 的第 2 次调用没有文本，也没有
            # tool_calls；Runner 保持 stop_reason=max_iterations。
            LLMResponse(content=None, finish_reason="stop"),
        ]
    )
    bus = MessageBus()
    agent_loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry()),
        max_iterations=1,
        session_manager=manager,
    )
    task = asyncio.create_task(agent_loop.run())
    try:
        outbound = await run_one_message(bus, "分析失败任务")
    finally:
        agent_loop.stop()
        await task

    assert outbound.metadata["error"] == "agent_incomplete"
    assert "本轮未完成，未保存到 Session。" in outbound.content
    assert manager.load().messages == []


@pytest.mark.asyncio
async def test_cli_history_clear_and_restart(tmp_path: Path) -> None:
    """CLI 启动摘要、/history、/clear 和重启恢复组成完整 V1 行为。"""
    workspace = make_workspace(tmp_path)
    data_root = tmp_path / "data"
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(
        ProviderProfile(
            name="mock",
            provider="mock",
            model="mock-add-demo",
        ),
        set_default=True,
    )
    console = ScriptedConsole(
        ["计算 2 + 3", "/history", "/clear", "/history", "/exit"]
    )
    cli = InteractiveCLI(
        console=console,
        profile_store=store,
        workspace=workspace,
        data_root=data_root,
    )
    await cli.run()

    text = "\n".join(console.output)
    assert "Mini Harness V1" in text
    assert f"Workspace: {workspace.resolve()}" in text
    assert "Session: cli:main" in text
    assert "History: 0 messages" in text
    assert "当前 Session 历史（4 条）" in text
    assert "已清空当前 Session 历史。" in text
    assert "当前 Session 历史为空。" in text

    manager = SessionManager(workspace, data_root=data_root)
    assert manager.load().messages == []
