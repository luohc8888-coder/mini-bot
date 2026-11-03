"""MessageBus 的入站、出站以及 asyncio.Queue FIFO 行为测试。"""

from __future__ import annotations

import asyncio

import pytest

from mini_harness.bus import InboundMessage, MessageBus, OutboundMessage


@pytest.mark.asyncio
async def test_inbound_message_roundtrip() -> None:
    """验证入站消息发布后可由 MessageBus 原样消费。"""
    bus = MessageBus()
    message = InboundMessage(
        content="计算 2 + 3",
        sender="cli",
        metadata={"request_id": "req-1"},
    )

    assert isinstance(bus.inbound, asyncio.Queue)

    await bus.publish_inbound(message)
    consumed = await bus.consume_inbound()

    assert consumed == message
    assert consumed.content == "计算 2 + 3"
    assert consumed.metadata["request_id"] == "req-1"
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_outbound_message_roundtrip() -> None:
    """验证出站消息发布后可由 MessageBus 原样消费。"""
    bus = MessageBus()
    message = OutboundMessage(
        content="结果是 5",
        recipient="cli",
        metadata={"stop_reason": "completed"},
    )

    assert isinstance(bus.outbound, asyncio.Queue)

    await bus.publish_outbound(message)
    consumed = await bus.consume_outbound()

    assert consumed == message
    assert consumed.content == "结果是 5"
    assert consumed.metadata["stop_reason"] == "completed"
    assert bus.outbound_size == 0


@pytest.mark.asyncio
async def test_inbound_queue_is_fifo() -> None:
    """验证入站 asyncio.Queue 严格按发布顺序消费（FIFO）。"""
    bus = MessageBus()
    expected = [f"message-{index}" for index in range(5)]

    for content in expected:
        await bus.publish_inbound(InboundMessage(content=content, sender="cli"))

    actual = [(await bus.consume_inbound()).content for _ in expected]
    assert actual == expected


@pytest.mark.asyncio
async def test_outbound_queue_is_fifo() -> None:
    """验证出站 asyncio.Queue 严格按发布顺序消费（FIFO）。"""
    bus = MessageBus()
    expected = [f"reply-{index}" for index in range(3)]

    for content in expected:
        await bus.publish_outbound(OutboundMessage(content=content, recipient="cli"))

    actual = [(await bus.consume_outbound()).content for _ in expected]
    assert actual == expected
