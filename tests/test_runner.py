"""MockProvider、AgentRunner 与 ToolRegistry 的单元测试。"""

from __future__ import annotations

from typing import Any

import pytest

from mini_harness.agent import AgentRunner, AgentRunSpec
from mini_harness.providers import LLMResponse, MockProvider, ToolCallRequest
from mini_harness.tools import AddTool, Tool, ToolRegistry, ToolResult


def make_registry() -> ToolRegistry:
    """创建只包含 V0 add 工具的注册表。"""
    registry = ToolRegistry()
    registry.register(AddTool())
    return registry


def add_tool_response(call_id: str = "call_add_1") -> LLMResponse:
    """构造第一次 Provider 响应：请求 add(a=2, b=3)。"""
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(id=call_id, name="add", arguments={"a": 2, "b": 3})
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_mock_provider_returns_scripted_tool_call() -> None:
    """验证 MockProvider 会返回预设 tool_calls，且不访问网络。"""
    provider = MockProvider([add_tool_response()])
    messages = [{"role": "user", "content": "计算 2 + 3"}]

    response = await provider.chat(messages=messages, tools=[])

    assert response.should_execute_tools is True
    assert response.has_tool_calls is True
    assert len(response.tool_calls) == 1
    tool_call = response.tool_calls[0]
    assert tool_call.id == "call_add_1"
    assert tool_call.name == "add"
    assert tool_call.arguments == {"a": 2, "b": 3}
    assert provider.call_count == 1
    assert provider.received_messages == [messages]


@pytest.mark.asyncio
async def test_runner_builds_messages_and_pairs_tool_call_id() -> None:
    """验证 Runner 的消息追加顺序、tool_call_id 配对和最终结果。"""
    provider = MockProvider(
        [
            add_tool_response(),
            MockProvider.final_from_tool_result(prefix="结果是"),
        ]
    )
    runner = AgentRunner(provider=provider, tools=make_registry())
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "计算 2 + 3"}],
        max_iterations=8,
    )

    result = await runner.run(spec)

    # 消息历史必须完整：user → assistant(tool_calls) → tool → assistant(final)。
    assert [message["role"] for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assistant_tool_message = result.messages[1]
    tool_result_message = result.messages[2]
    assert assistant_tool_message["tool_calls"][0] == {
        "id": "call_add_1",
        "name": "add",
        "arguments": {"a": 2, "b": 3},
    }
    assert tool_result_message["tool_call_id"] == "call_add_1"
    assert tool_result_message["name"] == "add"
    assert tool_result_message["content"] == "5"
    assert result.messages[3]["content"] == "结果是 5"

    # Runner 返回值。
    assert result.final_content == "结果是 5"
    assert result.tools_used == ["add"]
    assert result.stop_reason == "completed"

    # 第二次 Provider 调用确实看到了上一条 role=tool 的工具结果。
    assert provider.call_count == 2
    assert provider.received_messages[0] == [
        {"role": "user", "content": "计算 2 + 3"}
    ]
    assert provider.received_messages[1][-1]["role"] == "tool"
    assert provider.received_messages[1][-1]["tool_call_id"] == "call_add_1"
    assert provider.received_messages[1][-1]["content"] == "5"


@pytest.mark.asyncio
async def test_runner_keeps_multiple_tool_calls_paired() -> None:
    """验证同一轮的多个 tool_calls 按顺序各产生一条配对的 tool 结果。"""
    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_a", name="add", arguments={"a": 1, "b": 2}
                    ),
                    ToolCallRequest(
                        id="call_b", name="add", arguments={"a": 10, "b": 20}
                    ),
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="两次加法都完成了", finish_reason="stop"),
        ]
    )
    runner = AgentRunner(provider=provider, tools=make_registry())

    result = await runner.run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "计算两组加法"}],
            max_iterations=4,
        )
    )

    assert [message["role"] for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert [call["id"] for call in result.messages[1]["tool_calls"]] == [
        "call_a",
        "call_b",
    ]
    tool_messages = result.messages[2:4]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_a",
        "call_b",
    ]
    assert [message["content"] for message in tool_messages] == ["3", "30"]
    assert result.tools_used == ["add", "add"]
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_runner_respects_max_iterations() -> None:
    """验证 max_iterations 能阻止无限工具循环，并返回对应 stop_reason。"""
    provider = MockProvider([add_tool_response("call_once")])
    runner = AgentRunner(provider=provider, tools=make_registry())
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "计算 2 + 3"}],
        max_iterations=1,
    )

    result = await runner.run(spec)

    assert result.stop_reason == "max_iterations"
    assert result.final_content is None
    assert provider.call_count == 1
    assert [message["role"] for message in result.messages] == [
        "user",
        "assistant",
        "tool",
    ]
    # 即使是最后一轮，assistant tool_call 与 tool 结果也保持配对。
    assert result.messages[1]["tool_calls"][0]["id"] == "call_once"
    assert result.messages[2]["tool_call_id"] == "call_once"


@pytest.mark.asyncio
async def test_tool_registry_validation_and_unknown_tool() -> None:
    """验证缺失、类型错误、额外参数及未知工具都被转换为错误结果。"""
    registry = make_registry()

    missing = await registry.execute("add", {"a": 2})
    wrong_type = await registry.execute("add", {"a": "2", "b": 3})
    extra = await registry.execute("add", {"a": 2, "b": 3, "c": 4})
    unknown = await registry.execute("subtract", {"a": 2, "b": 3})
    valid = await registry.execute("add", {"a": 2, "b": 3})

    assert missing.is_error is True
    assert "缺少必填参数：b" in missing.content
    assert wrong_type.is_error is True
    assert "类型错误" in wrong_type.content
    assert extra.is_error is True
    assert "不支持参数：c" in extra.content
    assert unknown.is_error is True
    assert "未找到工具" in unknown.content
    assert valid.is_error is False
    assert valid.content == "5"
    assert valid.success is True


@pytest.mark.asyncio
async def test_tool_execution_error_does_not_crash_runner() -> None:
    """验证工具抛异常时 Runner 仍继续调用 Provider 并完成本轮。"""

    class ExplodingTool(Tool):
        """测试专用的失败工具。"""

        name = "explode"
        description = "always raises"
        parameters = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

        async def execute(self, **kwargs: Any) -> ToolResult:
            """模拟工具内部异常。"""
            raise RuntimeError("boom")

    registry = make_registry()
    registry.register(ExplodingTool())
    provider = MockProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCallRequest(id="call_bad", name="explode", arguments={})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="工具失败，但对话继续。", finish_reason="stop"),
        ]
    )
    runner = AgentRunner(provider=provider, tools=registry)

    result = await runner.run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "执行失败工具"}],
            max_iterations=4,
        )
    )

    assert result.stop_reason == "completed"
    assert result.final_content == "工具失败，但对话继续。"
    assert result.tools_used == []
    assert [message["role"] for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert "执行失败" in result.messages[2]["content"]
    assert "boom" in result.messages[2]["content"]
    assert provider.call_count == 2
