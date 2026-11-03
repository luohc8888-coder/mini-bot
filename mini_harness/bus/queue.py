"""基于 asyncio.Queue 的双向消息总线。

MessageBus 只负责排队和传递消息，不持有 Session、不调用 Provider，
也不判断消息业务含义。这样 CLI/通道与 Agent 核心可以解耦。
"""

from __future__ import annotations

import asyncio

from .events import InboundMessage, OutboundMessage


class MessageBus:
    """解耦输入通道与 AgentLoop 的异步消息总线。

    V0 使用两个独立的 ``asyncio.Queue``：入站队列和出站队列。队列均
    FIFO；V0 只有单消费者，因此不需要 Session Queue 或并发控制。
    """

    def __init__(self) -> None:
        """创建入站/出站队列。

        ``asyncio.Queue`` 可以在没有运行事件循环时构造，在第一次被
        await 时绑定到当前事件循环。
        """
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, message: InboundMessage) -> None:
        """把外部消息放入入站队列。

        Args:
            message: CLI/通道产生的 InboundMessage。

        Returns:
            None。

        上下游:
            CLI 调用本方法；AgentLoop.run 通过 ``consume_inbound`` 消费。
            这里 ``await put`` 会主动让出事件循环，使生产者和消费者可以
            异步协作，而不会忙等。
        """
        await self.inbound.put(message)

    async def consume_inbound(self) -> InboundMessage:
        """取出一条入站消息；队列为空时异步等待。

        Returns:
            队首的 InboundMessage。

        上下游:
            AgentLoop.run 调用本方法。``await`` 是关键：没有消息时协程
            必须挂起，不能通过轮询浪费 CPU。
        """
        return await self.inbound.get()

    async def publish_outbound(self, message: OutboundMessage) -> None:
        """把 Agent 的回复放入出站队列。

        Args:
            message: AgentLoop 产生的 OutboundMessage。

        Returns:
            None。

        上下游:
            AgentLoop._dispatch_inbound 调用本方法；CLI 通过
            ``consume_outbound`` 读取并输出。
        """
        await self.outbound.put(message)

    async def consume_outbound(self) -> OutboundMessage:
        """取出一条出站消息；队列为空时异步等待。

        Returns:
            队首的 OutboundMessage。

        上下游:
            CLI 调用本方法。``await`` 使 CLI 在 Agent 尚未完成时挂起，
            而不是反复查询队列长度。
        """
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """返回当前待处理的入站消息数量（仅用于观测/测试）。"""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """返回当前待消费的出站消息数量（仅用于观测/测试）。"""
        return self.outbound.qsize()
