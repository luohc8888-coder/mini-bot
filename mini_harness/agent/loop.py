"""AgentLoop：从 MessageBus 消费消息并驱动 AgentRunner。

V1 仍只有一个消费者，按到达顺序逐条处理。Loop 不直接调用 Provider，
也不执行工具；它负责输入适配、Session 历史读写、调用 Runner 并发布
OutboundMessage。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from mini_harness.bus.events import InboundMessage, OutboundMessage
from mini_harness.bus.queue import MessageBus
from mini_harness.providers import ProviderError
from mini_harness.session import SessionError, SessionManager

from .prompt import build_system_prompt
from .runner import AgentRunSpec, AgentRunner


class AgentLoop:
    """消息总线与 AgentRunner 之间的异步调度层。

    Args:
        bus: MessageBus，提供入站/出站队列。
        runner: AgentRunner，负责一次完整的工具调用循环。
        max_iterations: 每个消息交给 Runner 时的最大模型调用轮数。
        poll_interval: 无消息时等待多久检查一次停止标记。V0 用短超时
            实现可正常退出的常驻循环，不引入 Session 队列或后台任务。

    上下游:
        main.py 创建并 ``asyncio.create_task(agent_loop.run())``；CLI 通过
        MessageBus 投递 InboundMessage。

        ``session_manager`` 为可选参数：传入时启用 V1 Session 历史加载、
        更新和原子持久化；不传时保持 V0 的单轮无状态行为。
    """

    def __init__(
        self,
        bus: MessageBus,
        runner: AgentRunner,
        *,
        max_iterations: int = 32,
        poll_interval: float = 0.1,
        session_manager: SessionManager | None = None,
    ) -> None:
        """保存依赖并初始化运行状态。"""
        self.bus = bus
        self.runner = runner
        self._max_iterations = max_iterations
        self._poll_interval = poll_interval
        self._session_manager = session_manager
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
        """返回注入的 SessionManager；V0 模式下为 None。"""
        return self._session_manager

    async def run(self) -> None:
        """启动单消费者循环，直到调用 ``stop()``。

        Returns:
            None；通过设置内部 ``_running=False`` 正常退出。

        上下游:
            main.py 用 asyncio.create_task 启动本协程。为什么要 create_task：
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

                # await 单条消息处理，V0 顺序处理，不并发执行多个回合。
                await self._dispatch_inbound(message)
        finally:
            # 无论正常 stop 还是异常/cancel，都复位状态，便于测试判断
            # 任务已经结束，避免留下“看起来仍在运行”的标记。
            self._running = False

    def stop(self) -> None:
        """请求 AgentLoop 在下次检查时退出。

        V0 的 consume 最多等待 ``poll_interval`` 秒；调用 stop 后
        main.py 应 ``await loop_task``，确保协程真正结束再退出进程。
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
        """保持 V0 行为：每条入站消息都从一条 user 消息开始。"""
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": message.content}],
            max_iterations=self._max_iterations,
        )
        result = await self.runner.run(spec)
        return self._result_to_outbound(message, result)

    async def _process_with_session(
        self,
        message: InboundMessage,
    ) -> OutboundMessage:
        """加载历史、运行 Runner、保存完整历史，再返回本轮结果。"""
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
        input_messages = [
            *history,
            {"role": "user", "content": message.content},
        ]
        try:
            result = await self.runner.run(
                AgentRunSpec(
                    initial_messages=input_messages,
                    max_iterations=self._max_iterations,
                    system_prompt=build_system_prompt(manager.workspace),
                    finalize_on_max_iterations=True,
                )
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

        # 未完成/未收尾的回合不能污染 Session。只有正常完成，或预算耗尽
        # 后成功执行 finalize_on_max_iterations 的回合才允许持久化。
        if result.stop_reason not in {"completed", "max_iterations_finalized"}:
            return self._error_outbound(
                message,
                "本轮未完成，未保存到 Session。可以缩小任务范围后重试。",
                error="agent_incomplete",
            )

        # Runner 的约定是返回“输入消息 + 本轮新增消息”的完整列表。不能
        # 用 result.messages[len(history):] 直接切片，因为未来 ContextBuilder
        # 可能改变输入和输出的边界；这里先验证严格前缀关系。
        if (
            not isinstance(result.messages, list)
            or len(result.messages) < len(input_messages)
            or result.messages[: len(input_messages)] != input_messages
        ):
            return self._error_outbound(
                message,
                "Session 历史更新失败：Runner 返回的消息历史不是输入历史的"
                "完整前缀，已拒绝保存。",
                error="session_history_error",
            )

        session.messages = deepcopy(result.messages)
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
