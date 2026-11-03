"""消息总线：隔离输入输出通道与 Agent 核心。"""

from .events import InboundMessage, OutboundMessage
from .queue import MessageBus

__all__ = ["InboundMessage", "MessageBus", "OutboundMessage"]
