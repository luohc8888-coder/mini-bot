"""AgentRunner：真正执行“模型调用 → 工具调用 → 再调用模型”的核心循环。

Runner 与 Session、Memory、Checkpoint 等产品层能力无关。它只接收一份
初始消息和 Provider/ToolRegistry，执行有限轮迭代，并返回结构化结果。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from mini_harness.providers.base import (
    LLMProvider,
    Message,
    TextDeltaCallback,
    ToolDefinition,
    ToolStartCallback,
)
from mini_harness.tools.base import ToolResult
from mini_harness.tools.registry import ToolRegistry


@dataclass(slots=True)
class AgentRunSpec:
    """一次 Agent 运行的输入规格。

    Args:
        initial_messages: 进入 Runner 的初始消息历史，通常包含一条 user
            消息。Runner 会深拷贝它，不修改调用方持有的列表。
        max_iterations: 最多允许的“模型调用”轮数。即使模型持续请求工具，
            循环也不会无限执行。
        system_prompt: 每轮调用 Provider 时临时拼接的 system message；
            不会写入 result.messages 或 Session。
        finalize_on_max_iterations: 预算耗尽时是否额外请求一次不带工具的
            最终总结，避免只返回“未生成最终回答”。
        on_text_delta: 流式文本增量回调；Provider 产生可见文本时调用。
            该回调只影响展示，不会把中间 chunk 写入 result.messages。
        on_tool_start: 工具开始执行前的可选回调，参数为工具名。

    Returns:
        作为 ``AgentRunner.run`` 的入参。

    上下游:
        AgentLoop._process_message 根据 InboundMessage 构造本对象；
        AgentRunner.run 读取并复制 initial_messages。
    """

    initial_messages: list[Message] = field(default_factory=list)
    max_iterations: int = 32
    system_prompt: str = ""
    finalize_on_max_iterations: bool = True
    on_text_delta: TextDeltaCallback | None = None
    on_tool_start: ToolStartCallback | None = None


@dataclass(slots=True)
class AgentRunResult:
    """一次 Agent 运行的最终结果。

    Args:
        final_content: 最终文本回答；若达到 max_iterations 且收尾调用
            也没有产生回答，则为 None。
        messages: 完整消息历史，包括 user、assistant tool_calls、tool 结果
            以及最终 assistant 回答。
        tools_used: 成功执行的工具名列表（按执行顺序，允许重复）。
        stop_reason: ``"completed"`` 表示正常得到最终回答；
            ``"max_iterations_finalized"`` 表示预算耗尽后通过一次
            无工具的收尾调用得到了总结；``"max_iterations"`` 表示达到
            迭代上限且收尾也没有产生最终回答。
        streamed_final: 最终回答是否已经通过 on_text_delta 实时输出；
            CLI 据此避免把同一段回答再打印一次。

    Returns:
        交给 AgentLoop 生成 OutboundMessage 的结构化结果。

    上下游:
        AgentRunner._run_core 构造；AgentLoop._process_message 读取
        final_content、tools_used 和 stop_reason。
    """

    final_content: str | None
    messages: list[Message]
    tools_used: list[str] = field(default_factory=list)
    stop_reason: str = "completed"
    streamed_final: bool = False


class AgentRunner:
    """执行工具调用循环，但不感知 CLI、Session 或具体业务。

    Args:
        provider: 实现了 LLMProvider.chat 的模型提供者；V0 使用 MockProvider。
        tools: 工具注册表，负责导出工具定义并执行实际工具。

    这种职责划分保证业务判断位于 Provider/Tool，而不是硬编码在 Runner。
    """

    def __init__(self, provider: LLMProvider, tools: ToolRegistry) -> None:
        """注入 Provider 与工具注册表。

        Args:
            provider: LLMProvider 实例。
            tools: ToolRegistry 实例。

        Returns:
            None。

        上下游:
            main.py 和测试构造 AgentRunner，再交给 AgentLoop。
        """
        self.provider = provider
        self.tools = tools

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        """运行一次受 max_iterations 限制的 Agent 循环。

        Args:
            spec: 初始消息与最大迭代次数。

        Returns:
            AgentRunResult，包含最终回答、完整消息历史和停止原因。

        上下游:
            AgentLoop._process_message ``await`` 本方法。这里先复制消息，
            然后委托 ``_run_core`` 执行真正的循环；``await`` 是必须的，
            因为 Provider.chat 和工具执行都是异步操作，调用方需要等它们
            完成后才能生成 OutboundMessage。
        """
        # 深拷贝防止 Runner 追加消息时修改调用方传入的 initial_messages。
        messages: list[Message] = deepcopy(spec.initial_messages)
        return await self._run_core(spec, messages)

    @staticmethod
    def _provider_messages(
        spec: AgentRunSpec,
        messages: list[Message],
    ) -> list[Message]:
        """构造只用于本次 Provider 调用的消息列表。

        System Prompt 是运行时上下文，不属于会话历史。因此每轮调用都
        临时拼接它，调用结束后只把普通 messages 交给 Session 保存。
        """
        if not spec.system_prompt:
            return messages
        return [
            {"role": "system", "content": spec.system_prompt},
            *messages,
        ]

    async def _request(
        self,
        spec: AgentRunSpec,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
    ) -> tuple[Any, bool]:
        """调用 Provider，并在配置了回调时走流式接口。

        Returns:
            ``(response, streamed)``。streamed 表示本次响应是否真的产生过
            文本增量；它用于区分“流式已展示最终回答”和“需要 CLI 补打印”。
        """
        streamed = False

        def emit_delta(text: str) -> None:
            nonlocal streamed
            if not text:
                return
            streamed = True
            if spec.on_text_delta is not None:
                spec.on_text_delta(text)

        provider_messages = self._provider_messages(spec, messages)

        # 没有回调时保持原来的非流式路径，避免改变库调用方的行为。
        if spec.on_text_delta is None:
            response = await self.provider.chat(
                messages=provider_messages,
                tools=tools,
            )
            return response, False

        chat_stream = getattr(self.provider, "chat_stream", None)
        if callable(chat_stream):
            response = await chat_stream(
                messages=provider_messages,
                tools=tools,
                on_text_delta=emit_delta,
            )
        else:
            # 兼容只实现 chat 的第三方 Provider：完整拿到响应后一次性
            # 回调，至少保证 CLI 不会重复打印最终回答。
            response = await self.provider.chat(
                messages=provider_messages,
                tools=tools,
            )
            if response.content:
                emit_delta(response.content)
        return response, streamed

    async def _run_core(
        self,
        spec: AgentRunSpec,
        messages: list[Message],
    ) -> AgentRunResult:
        """执行核心的“模型 → 工具 → 模型”迭代。

        Args:
            spec: 运行规格，主要使用 max_iterations。
            messages: 已由 run 深拷贝的可变消息列表，本方法会就地追加消息。

        Returns:
            AgentRunResult。

        核心流程:
            1. 每轮先 ``await provider.chat`` 获取 LLMResponse。
            2. 若带 tool_calls：追加 assistant tool_calls 消息，逐个执行
               工具，追加 role=tool 结果，然后 ``continue`` 进入下一轮。
            3. 若不带 tool_calls：记录并追加最终 assistant 回答，``break``。
            4. 循环自然耗尽说明达到 max_iterations；若启用
               finalize_on_max_iterations，再请求一次不带工具的总结。

        上下游:
            AgentRunner.run 调用；provider.chat 的上游是 MockProvider，
            tools.execute 的上游是 ToolRegistry 和 AddTool。
        """
        final_content: str | None = None
        tools_used: list[str] = []
        stop_reason = "max_iterations"
        streamed_final = False

        # 工具定义在本轮运行中不会变化，提前导出一次即可。传给 Provider
        # 后，真实模型才能知道 add 的名称和参数 Schema。
        tool_definitions: list[ToolDefinition] = self.tools.get_definitions()

        for iteration in range(spec.max_iterations):
            # await Provider.chat：模拟/真实模型调用都是异步 I/O，必须等待
            # 响应后才能决定是否执行工具。这里每一轮都会把完整 messages
            # 传回去，因此第二轮能看到第一轮追加的工具结果。
            response, streamed = await self._request(
                spec,
                messages,
                tool_definitions,
            )

            if response.should_execute_tools:
                # 先把 assistant 的工具调用意图写入历史。没有这一步，
                # 后续 role=tool 消息就会失去对应的 tool_call_id。
                assistant_message: Message = {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        tool_call.to_message_dict()
                        for tool_call in response.tool_calls
                    ],
                }
                messages.append(assistant_message)  # 追加 assistant(tool_calls)

                # 逐个执行工具，保持顺序且确保每个调用都产生一条结果。
                # 多工具场景下不能在第一个结果后就进入下一轮，因此放在
                # 同一个 for 循环内全部执行完。
                for tool_call in response.tool_calls:
                    if spec.on_tool_start is not None:
                        spec.on_tool_start(tool_call.name)
                    # await 工具执行，保证本 result 在下一轮模型调用前
                    # 已经追加到 messages。
                    result: ToolResult = await self.tools.execute(
                        tool_call.name,
                        tool_call.arguments,
                    )
                    messages.append(  # 追加 role=tool，并用 id 配对
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result.content,
                        }
                    )
                    # 只有成功的工具才计入 tools_used；失败结果仍会回传给
                    # 模型，让模型有机会修正或向用户解释，而不是直接崩溃。
                    if not result.is_error:
                        tools_used.append(tool_call.name)

                # continue：本轮仍有工具调用，不能把 response.content 当作
                # 最终答案；进入下一轮再次询问 Provider。
                continue

            # 没有 tool_calls，说明 Provider 已给出最终回答。记录并追加
            # assistant 消息，保证 messages 能完整回放本次对话。
            final_content = (
                response.content if response.content is not None else ""
            )
            # 只有非空最终回答才值得跳过 CLI 的补打印；空响应仍走原有
            # “模型返回了空内容”的提示逻辑。
            streamed_final = bool(streamed and final_content.strip())
            # 空 assistant 消息不应进入后续上下文；否则真实模型可能收到
            # 空的 assistant content，导致下一轮行为不稳定。CLI 会单独
            # 显示“模型返回了空内容”的提示。
            if final_content.strip():
                messages.append({"role": "assistant", "content": final_content})
            stop_reason = "completed"
            # break：终止迭代循环，避免继续发起无意义的模型请求。
            break
        else:
            # for 循环没有被 break，说明 max_iterations 轮全部用完，且最后
            # 一轮仍在请求工具或没有给出最终回答。
            stop_reason = "max_iterations"

        if stop_reason == "max_iterations" and spec.finalize_on_max_iterations:
            # 预算耗尽时再给模型一次「不带工具、只总结」的机会。这样即使
            # 模型没来得及调用 write_file，也不会只给用户一句空话。
            response, streamed = await self._request(
                spec,
                [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "本轮工具调用预算已经用完。请只根据上面的对话和"
                            "工具结果，给出简洁的最终回答：说明已经完成了什么、"
                            "还缺什么、下一步建议。不要再调用工具。"
                        ),
                    },
                ],
                None,
            )
            if response.content and not response.tool_calls:
                final_content = response.content
                streamed_final = bool(streamed and final_content.strip())
                messages.append({"role": "assistant", "content": final_content})
                stop_reason = "max_iterations_finalized"

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            stop_reason=stop_reason,
            streamed_final=streamed_final,
        )
