"""不访问网络的 MockProvider，用于确定性演示和测试。

MockProvider 按照预设响应序列工作：第一次可以返回 add 工具调用，第二次
可以根据上一条 tool 消息生成最终回答。这样无需真实 LLM API 也能验证
AgentRunner 的完整工具调用闭环。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from copy import deepcopy

from .base import LLMProvider, LLMResponse, Message, ToolDefinition

# 响应序列中的一步既可以是固定 LLMResponse，也可以是一个根据当前 messages
# 动态构造响应的工厂函数。后者用于让第二次响应真正读取工具结果。
ResponseFactory = Callable[[list[Message]], LLMResponse]


class MockProvider(LLMProvider):
    """按顺序回放预设响应的 LLMProvider 实现。

    Args:
        responses: 响应或响应工厂组成的序列。每个 ``chat`` 调用消费一项。

    特性:
        - 完全离线，不访问网络。
        - 记录每次收到的 messages（深拷贝），便于测试断言。
        - 响应序列耗尽时抛出 RuntimeError，防止测试静默通过。
    """

    def __init__(self, responses: Sequence[LLMResponse | ResponseFactory]) -> None:
        """保存预设响应序列并初始化调用游标。

        Args:
            responses: 预设响应列表，不可为空（为空时首次 chat 会报错）。

        Returns:
            None。

        上下游:
            main.py 和测试构造该对象，然后注入 AgentRunner。Runner 不知道
            响应是固定值还是工厂函数，只依赖 LLMProvider 接口。
        """
        self._responses = list(responses)
        self._cursor = 0
        self.received_messages: list[list[Message]] = []

    @property
    def call_count(self) -> int:
        """已经完成的 chat 调用次数，便于测试迭代行为。"""
        return self._cursor

    @staticmethod
    def final_from_tool_result(prefix: str = "结果是") -> ResponseFactory:
        """构造一个“读取最后一条 tool 结果”的最终响应工厂。

        Args:
            prefix: 最终回答前缀，默认 ``"结果是"``。

        Returns:
            可放进 responses 序列的工厂函数；它会在被调用时扫描 messages，
            找到最近一条 ``role == "tool"`` 的消息并拼出最终文本。

        上下游:
            AgentRunner 第二次调用 ``chat`` 时，messages 末尾刚刚追加了
            tool 结果。工厂读取该结果，体现“第二次响应根据工具结果生成”，
            而不是在 AgentRunner 中硬编码业务答案。
        """

        def build(messages: list[Message]) -> LLMResponse:
            """从消息历史中提取最后一个工具结果并生成最终回答。"""
            for message in reversed(messages):
                if message.get("role") == "tool":
                    tool_content = str(message.get("content", ""))
                    return LLMResponse(
                        content=f"{prefix} {tool_content}".strip(),
                        finish_reason="stop",
                    )
            raise RuntimeError("MockProvider 未在 messages 中找到 tool 结果")

        return build

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        """返回响应序列中的下一项，模拟一次异步 LLM 调用。

        Args:
            messages: AgentRunner 传入的完整消息历史。
            tools: 工具定义列表；MockProvider 接受但当前不使用（响应已预设）。

        Returns:
            当前步骤的 LLMResponse。

        上下游:
            AgentRunner._run_core ``await`` 本方法。这里的 ``await
            asyncio.sleep(0)`` 主动让出一次事件循环，模拟真实 Provider 的
            异步 I/O 边界，同时让测试能够观察并发调度。
        """
        # 模拟异步 I/O：不访问网络，但保留 await 边界，避免假同步实现。
        await asyncio.sleep(0)

        # 深拷贝历史，确保测试断言的是“Provider 当时看到的快照”，后续
        # Runner 继续追加消息不会改变这份记录。
        self.received_messages.append(deepcopy(messages))

        if self._cursor >= len(self._responses):
            raise RuntimeError(
                f"MockProvider 响应序列已耗尽：已调用 {self._cursor} 次，"
                f"预设 {len(self._responses)} 次"
            )

        step = self._responses[self._cursor]
        self._cursor += 1

        # 工厂函数可以读取本轮 messages；固定 LLMResponse 则直接返回。
        if callable(step):
            return step(deepcopy(messages))
        return step
