"""AgentLoop：从 MessageBus 消费消息并驱动 AgentRunner。

V1 仍只有一个消费者，按到达顺序逐条处理。Loop 不直接调用 Provider，
也不执行工具；它负责输入适配、Session 历史读写、调用 Runner 并发布
OutboundMessage。
"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any

from mini_harness.bus.events import InboundMessage, OutboundMessage
from mini_harness.bus.queue import MessageBus
from mini_harness.providers import ProviderError
from mini_harness.providers.base import TextDeltaCallback, ToolStartCallback
from mini_harness.session import Session, SessionError, SessionManager

from .context_types import ContextBudgetExceeded, ContextConfig
from .context import ContextBuilder
from .runner import AgentRunSpec, AgentRunner

logger = logging.getLogger(__name__)


class AgentLoop:
    """消息总线与 AgentRunner 之间的异步调度层。

    Args:
        bus: MessageBus，提供入站/出站队列。
        runner: AgentRunner，负责一次完整的工具调用循环。
        max_iterations: 每个消息交给 Runner 时的最大模型调用轮数。
        poll_interval: 无消息时等待多久检查一次停止标记。使用短超时
            实现可正常退出的常驻循环，不引入 Session 队列或后台任务。
        on_text_delta: 可选的流式文本回调；CLI 用它实时渲染 Token。
        on_tool_start: 可选的工具开始回调；CLI 用来在流式文本之间穿插工具提示。
        context_builder: 可选的 ContextBuilder。传入时直接使用；不传且启用
            Session 时，Loop 会基于 ``session_manager.workspace`` 和
            ``runner.tools.get_definitions()`` 自动创建一个。
        context_config: 自动创建 ContextBuilder 时使用的预算配置；默认
            ``ContextConfig()``。
        stateless: 显式启用离线无状态运行，与 SessionManager 互斥。
        memory_store: 可选的 MemoryStore。传入时 ContextBuilder 会把
            ``MEMORY.md`` 注入 System Prompt；不传且启用 Session 时自动创建
            ``<workspace>/memory`` 下的默认 MemoryStore。

    上下游:
        CLI 创建并 ``asyncio.create_task(agent_loop.run())``；CLI 通过
        MessageBus 投递 InboundMessage。

        ``session_manager`` 为可选参数：传入时启用 V1 Session 历史加载、
        更新和原子持久化；无会话的离线任务须显式指定 ``stateless=True``。
    """

    def __init__(
        self,
        bus: MessageBus,
        runner: AgentRunner,
        *,
        max_iterations: int = 32,
        poll_interval: float = 0.1,
        session_manager: SessionManager | None = None,
        stateless: bool = False,
        context_builder: ContextBuilder | None = None,
        context_config: ContextConfig | None = None,
        memory_store: object | None = None,
        on_text_delta: TextDeltaCallback | None = None,
        on_tool_start: ToolStartCallback | None = None,
    ) -> None:
        """保存依赖并初始化运行状态。"""
        if session_manager is None and not stateless:
            raise ValueError("AgentLoop 需要 SessionManager；离线运行须显式设置 stateless=True")
        if session_manager is not None and stateless:
            raise ValueError("stateless=True 不能同时指定 SessionManager")
        self.bus = bus
        self.runner = runner
        self._max_iterations = max_iterations
        self._poll_interval = poll_interval
        self._session_manager = session_manager
        self._context_builder = context_builder
        self._context_config = context_config
        self._memory_store = memory_store
        if (
            self._memory_store is None
            and session_manager is not None
        ):
            # 延迟导入：memory 包与 session/agent 包互相独立，避免模块
            # 加载期的循环依赖。
            try:
                from mini_harness.memory.store import MemoryStore

                self._memory_store = MemoryStore(session_manager.workspace)
            except Exception:  # noqa: BLE001 - 记忆不可用不能阻止基础对话
                logger.exception("初始化长期记忆存储失败，本轮不注入 Memory")
        if self._context_builder is None and session_manager is not None:
            # V2 只在 Session 模式构建上下文；显式 stateless demo 路径使用
            # 单条 user 消息直接交给 Runner 的执行方式。
            effective_context_config = context_config
            if effective_context_config is None:
                effective_context_config = ContextConfig.from_provider(
                    runner.provider
                )
            self._context_builder = ContextBuilder(
                session_manager.workspace,
                effective_context_config,
                tool_definitions=runner.tools.get_definitions(),
                memory_store=self._memory_store,
            )
        elif self._context_builder is not None and self._memory_store is not None:
            # 注入的 Builder 也要共享 MemoryStore，保证 /context 与真实请求一致。
            if getattr(self._context_builder, "memory_store", None) is None:
                self._context_builder.memory_store = self._memory_store
        self._on_text_delta = on_text_delta
        self._on_tool_start = on_tool_start
        self._running = False
        # stop() 可能在 run() 所在 task 首次被调度前就被调用。单独的
        # _stop_requested 可以保留这个停止意图，避免 run() 启动时被
        # 无条件重置 _running=True 覆盖掉。
        self._stop_requested = False

    @property
    def running(self) -> bool:
        """Loop 当前是否处于运行状态。"""
        return self._running

    @property
    def session_manager(self) -> SessionManager | None:
        """返回注入的 SessionManager；显式 stateless 模式下为 None。"""
        return self._session_manager

    @property
    def context_builder(self) -> ContextBuilder | None:
        """返回当前使用的 ContextBuilder；显式 stateless 模式下为 None。"""
        return self._context_builder

    async def run(self) -> None:
        """启动单消费者循环，直到调用 ``stop()``。

        Returns:
            None；通过设置内部 ``_running=False`` 正常退出。

        上下游:
            CLI 用 asyncio.create_task 启动本协程。为什么要 create_task：
            ``run`` 是常驻循环，如果直接 await 会阻塞 CLI 投递消息和读取
            结果；创建任务后，CLI 与 AgentLoop 可以借助 asyncio.Queue 并发
            协作。

        实现说明:
            ``await bus.consume_inbound()`` 在没有消息时会永久挂起，因此
            外层用 ``asyncio.timeout`` 限时等待，超时后检查停止标记。
            这样 stop() 能在一个 poll_interval 内被观察到，且退出时不会遗留
            永远等待的 consume 协程。若 stop() 在 task 首次调度前调用，
            本方法会直接返回，不会启动消费者。
        """
        # 检查启动前的 stop()，避免覆盖调用方已经表达的停止意图。
        if self._stop_requested:
            return

        self._running = True
        try:
            while not self._stop_requested:
                try:
                    # await Queue.get：队列为空时挂起；timeout 只是给停止
                    # 标记一个检查窗口，并不创建独立消费者任务。
                    async with asyncio.timeout(self._poll_interval):
                        message = await self.bus.consume_inbound()
                except TimeoutError:
                    # 超时表示暂时没有消息；continue 重新检查运行标记。
                    continue

                # await 单条消息处理，单消费者顺序处理，不并发执行多个回合。
                await self._dispatch_inbound(message)
        finally:
            # 无论正常 stop 还是异常/cancel，都复位状态，便于测试判断
            # 任务已经结束，避免留下“看起来仍在运行”的标记。
            self._running = False

    def stop(self) -> None:
        """请求 AgentLoop 在下次检查时退出。

        无状态任务的 consume 最多等待 ``poll_interval`` 秒；调用 stop 后
        CLI 应 ``await loop_task``，确保协程真正结束再退出进程。
        即使 stop 在 run task 首次调度前调用，也会被保留并在启动时检查。
        """
        self._stop_requested = True
        self._running = False

    async def _dispatch_inbound(self, message: InboundMessage) -> OutboundMessage:
        """处理一条入站消息并发布对应出站消息。

        Args:
            message: 从 MessageBus 取出的用户消息。

        Returns:
            已发布的 OutboundMessage（返回值便于测试，不影响主流程）。

        上下游:
            上游是 ``AgentLoop.run`` 的 consume；下游是
            ``_process_message`` 与 ``MessageBus.publish_outbound``。
        """
        outbound = await self._process_message(message)
        # await 发布：确保在 dispatch 返回前消息已经进入出站队列，CLI
        # 的 await consume_outbound 才能稳定拿到结果。
        await self.bus.publish_outbound(outbound)
        return outbound

    async def _process_message(self, message: InboundMessage) -> OutboundMessage:
        """把 InboundMessage 转换成一次 Agent 运行并包装结果。

        Args:
            message: 用户输入消息。

        Returns:
            OutboundMessage，content 来自 Runner.final_content，metadata
            包含 stop_reason 和 tools_used。

        上下游:
            上游 ``_dispatch_inbound``；下游 ``AgentRunner.run``。本方法
            不直接调用 Provider 或 ToolRegistry，保持 Loop 的调度职责。
        """
        if self._session_manager is None:
            return await self._process_stateless(message)
        return await self._process_with_session(message)

    async def _process_stateless(self, message: InboundMessage) -> OutboundMessage:
        """每条入站消息都从一条 user 消息开始。"""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": message.content}],
            max_iterations=self._max_iterations,
            on_text_delta=self._on_text_delta,
            on_tool_start=self._on_tool_start,
            context_config=self._context_config,
        )
        result = await self.runner.run(spec)
        return self._result_to_outbound(message, result)

    async def _process_with_session(
        self,
        message: InboundMessage,
    ) -> OutboundMessage:
        """加载历史、用 ContextBuilder 构建请求、运行 Runner、保存新增消息。

        V2 的关键边界：

        - ContextBuilder 只生成临时 ``request_messages``（含 System Prompt
          与裁剪后的历史）；
        - Session 只追加“当前用户消息 + 本轮新增 assistant/tool 消息”；
        - 绝不把 ``request_messages`` 或 ``result.messages`` 整体写回 Session。
        """
        manager = self._session_manager
        assert manager is not None
        try:
            session = manager.load()
        except SessionError as exc:
            return self._error_outbound(
                message,
                f"Session 加载失败：{exc}",
                error="session_load_error",
            )
        except Exception as exc:  # noqa: BLE001 - 加载失败必须向 CLI 报告
            return self._error_outbound(
                message,
                f"Session 加载失败：{type(exc).__name__}: {exc}",
                error="session_load_error",
            )

        history = deepcopy(session.messages)
        current_message = {"role": "user", "content": message.content}
        builder = self._context_builder
        if builder is None:
            return self._error_outbound(
                message,
                "上下文构建失败：ContextBuilder 未初始化。",
                error="context_error",
            )

        try:
            built = builder.build_context(
                history,
                current_message,
                summary=session.summary,
                summary_boundary=session.summary_boundary,
            )
        except ContextBudgetExceeded as exc:
            # 超预算不截断当前输入、不清空历史、不调用 Provider。
            return self._error_outbound(
                message,
                f"上下文预算不足，本轮未发送给模型。{exc}",
                error="context_budget_exceeded",
            )
        except Exception as exc:  # noqa: BLE001 - 上下文构建失败必须报告
            return self._error_outbound(
                message,
                f"上下文构建失败：{type(exc).__name__}: {exc}",
                error="context_error",
            )

        request_messages = built.messages
        try:
            result = await self.runner.run(
                AgentRunSpec(
                    initial_messages=request_messages,
                    max_iterations=self._max_iterations,
                    finalize_on_max_iterations=True,
                    on_text_delta=self._on_text_delta,
                    on_tool_start=self._on_tool_start,
                    # 与 ContextBuilder 使用同一份预算配置：Runner 在每次
                    # Provider 请求前会再次检查并执行 Compact / 截断。
                    context_config=builder.config,
                    request_recorder=builder.record_provider_request,
                )
            )
        except ContextBudgetExceeded as exc:
            # 工具循环中 Compact 后仍无法压进输入预算：后续请求不会发送，
            # 但此前已经执行的工具副作用必须 append-only 落盘，不能让
            # Session 与文件系统事实脱节。
            self._persist_partial_failure(manager, current_message, exc)
            return self._error_outbound(
                message,
                f"上下文预算不足，本轮已停止后续模型请求；"
                f"此前已执行的工具记录已保存。{exc}",
                error="context_budget_exceeded",
            )
        except ProviderError as exc:
            return self._error_outbound(
                message,
                f"Provider 错误：{exc}",
                error="provider_error",
            )
        except Exception as exc:  # noqa: BLE001 - Loop 必须向 CLI 报告错误
            return self._error_outbound(
                message,
                f"Agent 运行失败：{type(exc).__name__}: {exc}",
                error="agent_error",
            )

        # 未完成/未收尾的回合：若已经执行过工具，必须保留执行 checkpoint，
        # 否则文件副作用与 Session 记录会永久分叉。没有工具副作用的纯失败
        # 回合仍保持不写 Session。
        if result.stop_reason not in {"completed", "max_iterations_finalized"}:
            new_messages = getattr(result, "new_messages", None)
            if (
                isinstance(new_messages, list)
                and new_messages
                and result.tools_used
            ):
                self._persist_result_failure(
                    manager, current_message, new_messages
                )
                outbound = self._error_outbound(
                    message,
                    "本轮工具执行预算已用完，未能生成最终回答；"
                    "此前已执行的工具记录已保存。",
                    error="context_budget_exceeded",
                )
                outbound.metadata["stop_reason"] = result.stop_reason
                outbound.metadata["tools_used"] = list(result.tools_used)
                return outbound
            return self._error_outbound(
                message,
                "本轮未完成，未保存到 Session。可以缩小任务范围后重试。",
                error="agent_incomplete",
            )

        # 使用 AgentRunResult 的显式边界校验，不依赖 len(history) 等不稳定
        # 假设。Runner 返回 messages = 输入 request_messages + new_messages。
        input_count = getattr(result, "input_message_count", None)
        new_messages = getattr(result, "new_messages", None)
        if (
            not isinstance(result.messages, list)
            or not isinstance(input_count, int)
            or input_count != len(request_messages)
            or not isinstance(new_messages, list)
            or len(result.messages) != input_count + len(new_messages)
            or result.messages[:input_count] != request_messages
            or result.messages[input_count:] != new_messages
        ):
            return self._error_outbound(
                message,
                "Session 历史更新失败：Runner 返回的消息历史不是输入历史的"
                "完整前缀，或新增消息边界不一致，已拒绝保存。",
                error="session_history_error",
            )

        # System Prompt 永远属于临时请求上下文；新消息中出现 system 说明
        # Runner 边界被破坏，宁可拒绝保存也不让提示词污染 Session。
        if any(
            not isinstance(item, dict) or item.get("role") == "system"
            for item in new_messages
        ):
            return self._error_outbound(
                message,
                "Session 历史更新失败：本轮新增消息包含 system 消息，"
                "已拒绝保存。",
                error="session_history_error",
            )

        # 完整旧历史 + 当前用户消息（只保存一次）+ 本轮新增执行消息。
        session.messages = [
            *deepcopy(history),
            deepcopy(current_message),
            *deepcopy(new_messages),
        ]
        try:
            manager.save(session)
        except SessionError as exc:
            # 保存失败时不发布正常完成结果；原 Session 文件由原子写入
            # 机制保护，不会被清空或部分覆盖。
            return self._error_outbound(
                message,
                f"Session 保存失败：{exc}",
                error="session_save_error",
            )
        except Exception as exc:  # noqa: BLE001 - 保存失败必须向 CLI 报告
            return self._error_outbound(
                message,
                f"Session 保存失败：{type(exc).__name__}: {exc}",
                error="session_save_error",
            )

        return self._result_to_outbound(
            message,
            result,
            history_count=len(session.messages),
        )

    def _persist_partial_failure(
        self,
        manager: Any,
        current_message: dict[str, Any],
        exc: ContextBudgetExceeded,
    ) -> None:
        """把预算失败前已执行的工具消息追加到 Session。

        只写 ``current user + Runner.new_messages``，绝不写本次请求拼接的
        System Prompt 或裁剪后的历史。没有新增消息时保持 Session 不变，
        与“首次请求就未发送”的既有语义一致。
        """
        partial_new = getattr(exc, "partial_new_messages", None)
        if not isinstance(partial_new, list) or not partial_new:
            return
        try:
            session = manager.load()
            manager.save(
                Session(
                    key=session.key,
                    messages=[
                        *deepcopy(session.messages),
                        deepcopy(current_message),
                        *deepcopy(partial_new),
                    ],
                    summary=session.summary,
                    summary_boundary=session.summary_boundary,
                    summary_checkpoint_id=session.summary_checkpoint_id,
                )
            )
        except Exception:  # noqa: BLE001 - 保存失败不能掩盖原始预算错误
            logger.exception("预算失败后保存已执行工具记录失败")

    def _persist_result_failure(
        self,
        manager: Any,
        current_message: dict[str, Any],
        new_messages: list[dict[str, Any]],
    ) -> None:
        """失败但已有工具副作用时，把当前 user 与本轮新增消息落盘。"""
        try:
            session = manager.load()
            manager.save(
                Session(
                    key=session.key,
                    messages=[
                        *deepcopy(session.messages),
                        deepcopy(current_message),
                        *deepcopy(new_messages),
                    ],
                    summary=session.summary,
                    summary_boundary=session.summary_boundary,
                    summary_checkpoint_id=session.summary_checkpoint_id,
                )
            )
        except Exception:  # noqa: BLE001 - 保存失败不能掩盖原始失败
            logger.exception("失败回合保存已执行工具记录失败")

    def _result_to_outbound(
        self,
        message: InboundMessage,
        result: Any,
        *,
        history_count: int | None = None,
    ) -> OutboundMessage:
        """把 AgentRunResult 转换成 OutboundMessage。"""
        content = result.final_content
        if content is None:
            content = "已达到最大迭代次数，未生成最终回答。"
        metadata: dict[str, Any] = {
            "stop_reason": result.stop_reason,
            "tools_used": list(result.tools_used),
            "streamed": bool(getattr(result, "streamed_final", False)),
            "inbound_metadata": dict(message.metadata),
        }
        if history_count is not None:
            metadata["history_count"] = history_count
        return OutboundMessage(
            content=content,
            recipient=message.sender,
            metadata=metadata,
        )

    def _error_outbound(
        self,
        message: InboundMessage,
        content: str,
        *,
        error: str,
    ) -> OutboundMessage:
        """构造可展示给 CLI 的错误出站消息，不伪装成正常完成。"""
        return OutboundMessage(
            content=content,
            recipient=message.sender,
            metadata={
                "error": error,
                "stop_reason": error,
                "tools_used": [],
                "inbound_metadata": dict(message.metadata),
            },
        )
