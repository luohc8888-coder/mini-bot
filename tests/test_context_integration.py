"""V2 Context Engineering 端到端集成测试。

覆盖：Session 完整历史保留、System Prompt 不落盘、AGENTS.md 每轮刷新、
上下文裁剪、Tool Calling 跨重启完整性、超预算保护与 /context 调试命令。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mini_harness.agent import (
    AgentLoop,
    AgentRunSpec,
    AgentRunner,
    ContextConfig,
)
from mini_harness.bus import InboundMessage, MessageBus
from mini_harness.cli import Console, InteractiveCLI, ProfileStore, ProviderProfile
from mini_harness.providers import LLMResponse, MockProvider, ToolCallRequest
from mini_harness.session import DEFAULT_SESSION_KEY, Session, SessionManager
from mini_harness.tools import AddTool, ToolRegistry


class ScriptedConsole(Console):
    """按顺序返回输入并记录输出，供 CLI 集成测试使用。"""

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

    @property
    def text(self) -> str:
        return "\n".join(self.output)


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def registry() -> ToolRegistry:
    result = ToolRegistry()
    result.register(AddTool())
    return result


def make_tool_provider() -> MockProvider:
    return MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_add_v2",
                        name="add",
                        arguments={"a": 2, "b": 3},
                    )
                ],
                finish_reason="tool_calls",
            ),
            MockProvider.final_from_tool_result(prefix="结果是"),
        ]
    )


async def run_one_message(
    agent_loop: AgentLoop,
    bus: MessageBus,
    content: str,
) -> Any:
    await bus.publish_inbound(InboundMessage(content=content, sender="cli"))
    return await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)


async def run_loop_once(
    manager: SessionManager,
    provider: MockProvider,
    content: str,
    *,
    context_config: ContextConfig | None = None,
    max_iterations: int = 32,
) -> Any:
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry()),
        session_manager=manager,
        context_config=context_config,
        max_iterations=max_iterations,
    )
    task = asyncio.create_task(loop.run())
    try:
        return await run_one_message(loop, bus, content)
    finally:
        loop.stop()
        await task


def simple_history(turns: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index in range(turns):
        messages.append({"role": "user", "content": f"历史问题 {index}"})
        messages.append({"role": "assistant", "content": f"历史回答 {index}"})
    return messages


@pytest.mark.asyncio
async def test_context_trimming_does_not_delete_session_history(
    tmp_path: Path,
) -> None:
    """ContextBuilder 裁掉旧消息后，Session 仍保存完整旧历史。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    old_history = simple_history(40)
    manager.save(Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(old_history)))

    provider = MockProvider([LLMResponse(content="本轮回答")])
    config = ContextConfig(max_context_tokens=1000, reserve_output_tokens=100)
    outbound = await run_loop_once(
        manager,
        provider,
        "最新问题",
        context_config=config,
    )

    assert outbound.content == "本轮回答"
    sent = provider.received_messages[0]
    assert sent[0]["role"] == "system"
    assert sent[-1] == {"role": "user", "content": "最新问题"}
    # 模型请求只拿到一部分历史。
    assert len(sent) - 2 < len(old_history)

    saved = manager.load().messages
    assert saved[: len(old_history)] == old_history
    assert saved[-2] == {"role": "user", "content": "最新问题"}
    assert saved[-1] == {"role": "assistant", "content": "本轮回答"}
    assert all(message["role"] != "system" for message in saved)


@pytest.mark.asyncio
async def test_system_prompt_never_persisted_across_two_turns(
    tmp_path: Path,
) -> None:
    """连续两轮后 session.json 中没有 System Prompt。"""
    manager = SessionManager(
        make_workspace(tmp_path),
        data_root=tmp_path / "data",
    )
    provider = MockProvider(
        [
            LLMResponse(content="第一轮回答"),
            LLMResponse(content="第二轮回答"),
        ]
    )
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry()),
        session_manager=manager,
    )
    task = asyncio.create_task(loop.run())
    try:
        first = await run_one_message(loop, bus, "第一轮问题")
        second = await run_one_message(loop, bus, "第二轮问题")
    finally:
        loop.stop()
        await task

    assert first.content == "第一轮回答"
    assert second.content == "第二轮回答"
    saved = manager.load().messages
    assert [message["role"] for message in saved] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert all(message["role"] != "system" for message in saved)
    assert sum(
        1 for message in saved if message.get("content") == "第一轮问题"
    ) == 1
    # 两次 Provider 请求都以 system 开头，但该 system 不在 Session 中。
    for received in provider.received_messages:
        assert received[0]["role"] == "system"


@pytest.mark.asyncio
async def test_agents_md_refreshes_between_turns_and_is_not_persisted(
    tmp_path: Path,
) -> None:
    """AGENTS.md 每轮重新读取；内容只进临时 System Prompt，不进 Session。"""
    workspace = make_workspace(tmp_path)
    agents = workspace / "AGENTS.md"
    agents.write_text("PROJECT-RULE: 第一版规则", encoding="utf-8")
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    provider = MockProvider(
        [
            LLMResponse(content="第一轮"),
            LLMResponse(content="第二轮"),
        ]
    )
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry()),
        session_manager=manager,
    )
    task = asyncio.create_task(loop.run())
    try:
        await run_one_message(loop, bus, "第一问")
        agents.write_text("PROJECT-RULE: 第二版规则", encoding="utf-8")
        await run_one_message(loop, bus, "第二问")
    finally:
        loop.stop()
        await task

    first_system = provider.received_messages[0][0]["content"]
    second_system = provider.received_messages[1][0]["content"]
    assert "第一版规则" in first_system
    assert "第二版规则" not in first_system
    assert "第二版规则" in second_system
    assert "第一版规则" not in second_system

    saved = manager.load().messages
    assert all("PROJECT-RULE" not in str(message.get("content")) for message in saved)


@pytest.mark.asyncio
async def test_tool_call_messages_survive_restart_with_ids(
    tmp_path: Path,
) -> None:
    """工具调用消息与 tool_call_id 持久化后跨进程重启仍完整。"""
    workspace = make_workspace(tmp_path)
    data_root = tmp_path / "data"
    manager = SessionManager(workspace, data_root=data_root)

    first = await run_loop_once(manager, make_tool_provider(), "计算 2 + 3")
    assert first.content == "结果是 5"

    first_history = manager.load().messages
    assert [message["role"] for message in first_history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert first_history[1]["tool_calls"][0]["id"] == "call_add_v2"
    assert first_history[2]["tool_call_id"] == "call_add_v2"

    # 重新创建 Manager / Provider / Loop，模拟进程重启。
    restored_manager = SessionManager(workspace, data_root=data_root)
    second_provider = MockProvider(
        [MockProvider.final_from_tool_result(prefix="刚才的结果是")]
    )
    second = await run_loop_once(
        restored_manager,
        second_provider,
        "刚才的结果是多少？",
    )

    assert second.content == "刚才的结果是 5"
    sent = second_provider.received_messages[0]
    assert sent[0]["role"] == "system"
    assert sent[1:5] == first_history
    assert sent[2]["tool_calls"][0]["id"] == "call_add_v2"
    assert sent[3]["tool_call_id"] == "call_add_v2"
    assert sent[-1] == {"role": "user", "content": "刚才的结果是多少？"}

    final_history = restored_manager.load().messages
    assert len(final_history) == 6
    assert final_history[2]["tool_call_id"] == "call_add_v2"


@pytest.mark.asyncio
async def test_context_budget_exceeded_keeps_session_and_skips_provider(
    tmp_path: Path,
) -> None:
    """当前输入超预算时明确报错、不调用 Provider、不改历史。"""
    manager = SessionManager(
        make_workspace(tmp_path),
        data_root=tmp_path / "data",
    )
    old_history = simple_history(3)
    manager.save(Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(old_history)))
    provider = MockProvider([LLMResponse(content="不应被调用")])
    before_file = manager.path.read_text(encoding="utf-8")

    outbound = await run_loop_once(manager, provider, "超" * 8000)

    assert outbound.metadata["error"] == "context_budget_exceeded"
    assert "上下文预算不足" in outbound.content
    assert provider.call_count == 0
    assert manager.load().messages == old_history
    assert manager.path.read_text(encoding="utf-8") == before_file


@pytest.mark.asyncio
async def test_runner_new_messages_boundary_is_explicit() -> None:
    """Runner 明确区分输入 request_messages 和本轮新增消息。"""
    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_boundary",
                        name="add",
                        arguments={"a": 1, "b": 2},
                    )
                ],
                finish_reason="tool_calls",
            ),
            MockProvider.final_from_tool_result(prefix="结果"),
        ]
    )
    initial = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "计算 1 + 2"},
    ]
    result = await AgentRunner(provider, registry()).run(
        AgentRunSpec(initial_messages=initial, max_iterations=4)
    )

    assert result.input_message_count == 2
    assert [message["role"] for message in result.new_messages] == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert all(message["role"] != "system" for message in result.new_messages)
    assert result.messages == [*initial, *result.new_messages]
    assert result.messages[: result.input_message_count] == initial


@pytest.mark.asyncio
async def test_cli_context_command_reports_real_stats_without_saving(
    tmp_path: Path,
) -> None:
    """/context 显示真实统计，不触发 LLM，也不创建 Session 文件。"""
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
    console = ScriptedConsole(["/context", "/exit"])
    cli = InteractiveCLI(
        console=console,
        profile_store=store,
        workspace=workspace,
        data_root=data_root,
    )

    await cli.run()

    text = console.text
    assert "Mini Harness V2" in text
    assert "上下文信息" in text
    assert f"工作目录：{workspace.resolve()}" in text
    assert "AGENTS.md：未找到" in text
    assert "会话消息数：0" in text
    assert "已选历史消息数：0" in text
    assert "已丢弃历史消息数：0" in text
    assert "估算输入 Token：" in text
    assert "配置输入预算：7168" in text
    assert "请求失败" not in text
    assert not SessionManager(workspace, data_root=data_root).path.exists()


@pytest.mark.asyncio
async def test_cli_context_command_shows_agents_and_trimming(
    tmp_path: Path,
) -> None:
    """/context 能展示 AGENTS.md 状态以及真实的选择/丢弃数量。"""
    workspace = make_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text(
        "# 项目规则\n\n- context test rule\n",
        encoding="utf-8",
    )
    data_root = tmp_path / "data"
    manager = SessionManager(workspace, data_root=data_root)
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=simple_history(30),
        )
    )

    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(
        ProviderProfile(
            name="mock",
            provider="mock",
            model="mock-add-demo",
        ),
        set_default=True,
    )
    console = ScriptedConsole(["/context", "/exit"])
    cli = InteractiveCLI(
        console=console,
        profile_store=store,
        workspace=workspace,
        data_root=data_root,
        context_config=ContextConfig(
            # 新增 find_files/apply_patch 等工具 Schema 后，固定开销约 2.6K。
            # 保留少量历史预算，继续验证选择与裁剪的实际行为。
            max_context_tokens=3000,
            reserve_output_tokens=100,
        ),
    )

    await cli.run()

    text = console.text
    assert "AGENTS.md：已加载" in text
    assert "会话消息数：60" in text
    assert "已选历史消息数：" in text
    assert "已丢弃历史消息数：" in text
    selected_line = next(
        line
        for line in console.output
        if line.startswith("已选历史消息数：")
    )
    dropped_line = next(
        line
        for line in console.output
        if line.startswith("已丢弃历史消息数：")
    )
    selected = int(selected_line.split("：")[1].strip())
    dropped = int(dropped_line.split("：")[1].strip())
    assert 0 < selected < 60
    assert selected + dropped == 60


@pytest.mark.asyncio
async def test_cli_last_prompt_prints_actual_request_messages(tmp_path: Path) -> None:
    """/last-prompt 展示当前进程最近一次真实发给模型的请求。"""
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
        ["计算 2 + 3", "/context", "/last-prompt", "/exit"]
    )
    cli = InteractiveCLI(
        console=console,
        profile_store=store,
        workspace=workspace,
        data_root=data_root,
    )

    await cli.run()

    text = console.text
    assert "上一次发给模型的请求消息" in text
    assert "[1] system" in text
    assert "[2] user" in text
    assert "计算 2 + 3" in text
    assert "系统提示词：已加载" in text
    assert "工具定义（不属于 messages" in text
    assert '"name": "add"' in text
