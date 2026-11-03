"""MessageBus 的事件数据结构。

InboundMessage 表示从 CLI/通道进入 Agent 的消息；OutboundMessage 表示
Agent 处理完成、准备返回给 CLI/通道的消息。两者只承载传输数据，不负责
Session，也不会触发 LLM 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class InboundMessage:
    """从外部通道进入 MessageBus 的用户消息。

    Args:
        content: 用户输入正文，例如 ``"计算 2 + 3"``。
        sender: 消息来源标识，V0 固定为 CLI。
        metadata: 与消息一起传递的可选元数据，V0 不依赖其内容。

    Returns:
        一个不可变的“消息意图”载体（字段本身仍可读写）。

    上下游:
        CLI 创建该对象并调用 ``MessageBus.publish_inbound``；AgentLoop
        通过 ``MessageBus.consume_inbound`` 取得它。
    """

    content: str
    sender: str = "cli"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundMessage:
    """Agent 处理完成后发回外部通道的消息。

    Args:
        content: 最终回答正文，例如 ``"结果是 5"``。
        recipient: 回复目标，V0 沿用入站消息的 sender。
        metadata: 运行摘要（stop_reason、tools_used 等），便于 CLI 调试。

    Returns:
        传输给 CLI 的回复对象。

    上下游:
        AgentLoop 创建该对象并调用 ``MessageBus.publish_outbound``；
        CLI 通过 ``MessageBus.consume_outbound`` 取出并打印。
    """

    content: str
    recipient: str = "cli"
    metadata: dict[str, Any] = field(default_factory=dict)
