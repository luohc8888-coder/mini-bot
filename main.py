"""Mini Harness V0 CLI 入口。

运行:
    uv run python main.py

它演示一次完整的工具调用闭环：
InboundMessage → MessageBus → AgentLoop → AgentRunner → MockProvider
→ ToolRegistry(add) → MockProvider → AgentRunResult → OutboundMessage
→ MessageBus → CLI。
"""

from __future__ import annotations

import asyncio

from mini_harness.agent import AgentLoop, AgentRunner
from mini_harness.bus import InboundMessage, MessageBus
from mini_harness.providers import LLMResponse, MockProvider, ToolCallRequest
from mini_harness.tools import AddTool, ToolRegistry


def build_demo_provider() -> MockProvider:
    """构造演示用的两步 MockProvider。

    Returns:
        MockProvider：第一次返回 add(2, 3) 工具调用，第二次从 messages
        中读取 role=tool 的结果并生成 ``"结果是 5"``。

    上下游:
        main() 将本 Provider 注入 AgentRunner。业务规则（调用哪个工具、
        如何回答）都留在 Provider/工具层，Runner 保持通用。
    """
    return MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_add_1",
                        name="add",
                        arguments={"a": 2, "b": 3},
                    )
                ],
                finish_reason="tool_calls",
            ),
            # 第二个预设响应是工厂函数：它读取 Runner 第二次调用时传入的
            # tool 消息，因此最终答案确实由工具结果驱动。
            MockProvider.final_from_tool_result(prefix="结果是"),
        ]
    )


async def main() -> None:
    """运行一次 V0 工具调用闭环并打印最终答案。

    Returns:
        None。

    上下游:
        创建 MessageBus / MockProvider / ToolRegistry / AgentRunner /
        AgentLoop；CLI 发布 ``"计算 2 + 3"``，再等待并打印 OutboundMessage。

    asyncio 说明:
        ``AgentLoop.run`` 是常驻循环，必须用 ``asyncio.create_task`` 并发
        启动；如果直接 await，就会永远阻塞，无法执行后面的发布消息步骤。
        退出时必须调用 ``agent_loop.stop()`` 并 ``await loop_task``，确保
        没有遗留无限等待的任务。
    """
    bus = MessageBus()

    registry = ToolRegistry()
    registry.register(AddTool())

    provider = build_demo_provider()
    runner = AgentRunner(provider=provider, tools=registry)
    agent_loop = AgentLoop(bus=bus, runner=runner, max_iterations=8)

    # 后台任务：让 AgentLoop 开始等待入站消息，同时主协程可以继续投递。
    loop_task = asyncio.create_task(agent_loop.run())

    try:
        # await 发布：把用户消息放入 asyncio.Queue，AgentLoop 才能消费。
        await bus.publish_inbound(
            InboundMessage(content="计算 2 + 3", sender="cli")
        )

        # 等待出站消息，并设置超时防止演示无限挂起。
        outbound = await asyncio.wait_for(
            bus.consume_outbound(),
            timeout=5.0,
        )
        print(outbound.content)
    finally:
        # 请求正常停止，并等待 run 协程退出（最多等待一个 poll interval）。
        agent_loop.stop()
        await loop_task


if __name__ == "__main__":
    asyncio.run(main())
