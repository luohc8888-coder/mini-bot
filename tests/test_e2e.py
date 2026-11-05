"""V0 完整端到端测试。

验证真实执行的链路：
InboundMessage → MessageBus → AgentLoop → AgentRunner → MockProvider
→ ToolRegistry(add) → MockProvider → AgentRunResult → OutboundMessage
→ MessageBus → CLI。
"""

from __future__ import annotations

import asyncio

import pytest

from mini_harness.cli.main import main as cli_main
from mini_harness.agent import AgentLoop, AgentRunner
from mini_harness.bus import InboundMessage, MessageBus
from mini_harness.providers import LLMResponse, MockProvider, ToolCallRequest
from mini_harness.tools import AddTool, ToolRegistry


def build_pipeline() -> tuple[
    MessageBus, AgentLoop, MockProvider, asyncio.Task[None]
]:
    """组装完整 V0 管线并启动 AgentLoop 后台任务。

    Returns:
        (bus, agent_loop, provider, loop_task)。调用方负责在 finally 中
        ``agent_loop.stop()`` 并 ``await loop_task``，避免遗留异步任务。
    """
    bus = MessageBus()
    registry = ToolRegistry()
    registry.register(AddTool())
    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_e2e_add",
                        name="add",
                        arguments={"a": 2, "b": 3},
                    )
                ],
                finish_reason="tool_calls",
            ),
            MockProvider.final_from_tool_result(prefix="结果是"),
        ]
    )
    runner = AgentRunner(provider=provider, tools=registry)
    agent_loop = AgentLoop(stateless=True, bus=bus, runner=runner, max_iterations=8)

    # create_task 让常驻 AgentLoop 与测试主协程并发运行；直接 await
    # agent_loop.run() 会永久阻塞，无法继续投递入站消息。
    loop_task = asyncio.create_task(agent_loop.run())
    return bus, agent_loop, provider, loop_task


@pytest.mark.asyncio
async def test_full_agent_tool_call_pipeline() -> None:
    """验证真实组件协作完成一次 add 工具调用闭环。"""
    bus, agent_loop, provider, loop_task = build_pipeline()

    try:
        await bus.publish_inbound(
            InboundMessage(content="计算 2 + 3", sender="cli")
        )
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=2.0)
    finally:
        # 正常停止而不是 cancel：验证 run() 可以自行结束，不留下无限等待的
        # consume_inbound 任务。
        agent_loop.stop()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert outbound.content == "结果是 5"
    assert outbound.recipient == "cli"
    assert outbound.metadata["stop_reason"] == "completed"
    assert outbound.metadata["tools_used"] == ["add"]

    # 两次模型调用：第一次请求工具，第二次读取工具结果并回答。
    assert provider.call_count == 2
    assert provider.received_messages[0] == [
        {"role": "user", "content": "计算 2 + 3"}
    ]
    second_call_messages = provider.received_messages[1]
    assert [message["role"] for message in second_call_messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert second_call_messages[2]["tool_call_id"] == "call_e2e_add"
    assert second_call_messages[2]["content"] == "5"

    # AgentLoop 协程已正常结束。
    assert loop_task.done()
    assert loop_task.cancelled() is False


@pytest.mark.asyncio
async def test_stop_before_run_prevents_loop_from_starting() -> None:
    """验证在 run task 首次调度前调用 stop()，Loop 不会启动消费者。"""
    bus = MessageBus()
    registry = ToolRegistry()
    registry.register(AddTool())
    runner = AgentRunner(provider=MockProvider([]), tools=registry)
    agent_loop = AgentLoop(stateless=True, bus=bus, runner=runner)

    # 模拟 create_task 之后、协程尚未得到执行机会就请求停止的时序。
    agent_loop.stop()
    await asyncio.wait_for(agent_loop.run(), timeout=0.5)

    assert agent_loop.running is False
    assert bus.inbound_size == 0


def test_cli_main_prints_expected_answer(capsys: pytest.CaptureFixture[str]) -> None:
    """验证实际 CLI 演示入口打印最终答案，且退出后没有挂起任务。"""
    asyncio.run(cli_main(["--demo"]))
    captured = capsys.readouterr()
    assert captured.out.strip() == "结果是 5"


def test_loop_requires_explicit_stateless_mode() -> None:
    runner = AgentRunner(provider=MockProvider([]), tools=ToolRegistry())
    with pytest.raises(ValueError, match="stateless=True"):
        AgentLoop(bus=MessageBus(), runner=runner)


def test_loop_rejects_session_and_stateless_together(tmp_path) -> None:
    from mini_harness.session import SessionManager

    runner = AgentRunner(provider=MockProvider([]), tools=ToolRegistry())
    with pytest.raises(ValueError, match="不能同时指定"):
        AgentLoop(
            bus=MessageBus(), runner=runner, stateless=True,
            session_manager=SessionManager(tmp_path, data_root=tmp_path / "data"),
        )
