"""Provider 抽象层的数据结构与接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

# V0 的消息使用普通 Python dict，便于后续替换 Provider。assistant 消息中的
# tool_calls 采用简化的 ``{id, name, arguments}`` 结构，而不是 OpenAI 的
# ``type/function`` 完整包装；Runner 只依赖这层最小结构。
Message = dict[str, Any]
# ToolRegistry.get_definitions() 返回的工具 Schema（OpenAI function 格式）。
ToolDefinition = dict[str, Any]
# 流式文本增量回调；Provider 每产生一段可见文本就调用一次。
TextDeltaCallback = Callable[[str], None]
# 工具开始执行回调，CLI 可用于在流式输出中插入工具提示。
ToolStartCallback = Callable[[str], None]


class ProviderError(RuntimeError):
    """Provider 请求失败时抛出的统一异常。

    CLI 会捕获该异常并打印可读错误，而不是让整个交互进程退出。
    具体错误信息应包含 Provider、HTTP 状态码（如有）和响应摘要。
    """


@dataclass(slots=True)
class ToolCallRequest:
    """Provider 返回的一次工具调用请求。

    Args:
        id: Provider 生成的调用 ID，用于和 tool 结果一一配对。
        name: 要执行的工具名，例如 ``"add"``。
        arguments: 工具参数，必须是 JSON 对象（Python dict）。

    Returns:
        一个传给 AgentRunner 的结构化工具调用意图。

    上下游:
        MockProvider.chat 产生该对象；AgentRunner._run_core 读取它，
        调用 ToolRegistry.execute，并把 id 写入 assistant/tool 消息。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_message_dict(self) -> dict[str, Any]:
        """转换成写入 messages 的 assistant tool_call 结构。

        Returns:
            ``{"id": ..., "name": ..., "arguments": {...}}``。

        使用深拷贝，避免后续工具执行或测试修改消息历史时反向污染
        Provider 返回的原始参数。
        """
        return {
            "id": self.id,
            "name": self.name,
            "arguments": deepcopy(self.arguments),
        }


@dataclass(slots=True)
class LLMResponse:
    """一次 LLM（或 MockProvider）调用的响应。

    Args:
        content: 文本回答；工具调用轮次可以为 None。
        tool_calls: 本轮请求执行的工具列表；空列表表示最终回答。
        finish_reason: Provider 给出的结束原因，V0 仅作记录。
        reasoning_content: 需要回放的 Provider 推理/续接内容（如 DeepSeek
            thinking）；只用于协议续接，不展示给最终用户。

    Returns:
        供 AgentRunner 判断是否继续工具循环的响应对象。

    上下游:
        Provider.chat 返回给 AgentRunner._run_core。Runner 根据
        ``should_execute_tools`` 决定追加 tool 消息并 ``continue``，还是
        记录最终回答并 ``break``。
    """

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    reasoning_content: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        """是否存在待执行的工具调用。"""
        return bool(self.tool_calls)

    @property
    def should_execute_tools(self) -> bool:
        """Runner 是否应进入工具执行分支。

        V0 的规则很简单：只要响应带有 tool_calls 就执行。真实 Provider
        可能需要同时检查 finish_reason，但这属于后续版本。
        """
        return self.has_tool_calls


class LLMProvider(ABC):
    """LLM Provider 抽象接口。

    V0 只定义最小能力：给定消息历史和工具定义，返回一个 LLMResponse。
    MockProvider 实现该接口但不访问网络；未来可替换为真实 Provider。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        """根据消息历史生成下一步响应。

        Args:
            messages: 完整对话历史，包含 user/assistant/tool 消息。
            tools: 可供模型选择的工具定义；None 表示不提供工具。

        Returns:
            LLMResponse，其中可能包含 tool_calls，也可能是最终文本。

        上下游:
            AgentRunner._run_core 在每轮迭代中 await 本方法。网络型实现
            必须把 I/O 放在 await 后面，避免阻塞事件循环。
        """
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        on_text_delta: TextDeltaCallback | None = None,
    ) -> LLMResponse:
        """带文本增量回调的聊天接口。

        默认实现退化为一次普通 chat：完整回答生成后把 content 一次性
        回调给调用方。支持 SSE 的 Provider 应覆盖本方法，实现真正的
        边接收边回调。

        Args:
            messages: 完整对话历史。
            tools: 工具定义。
            on_text_delta: 文本增量回调；None 表示不需要流式。

        Returns:
            完整的 LLMResponse，工具调用已经聚合完成。
        """
        response = await self.chat(messages=messages, tools=tools)
        if on_text_delta is not None and response.content:
            on_text_delta(response.content)
        return response
