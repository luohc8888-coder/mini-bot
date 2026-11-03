"""Mini Harness V0。

一个最小可运行的 Agent Harness，用于从零理解 Agent Loop、Provider、
ToolRegistry 以及消息总线之间的协作关系。V0 只实现工具调用闭环，
不包含 Session、Context Engineering、Memory、Hooks、Checkpoint 或 MCP。
"""

from .agent import AgentLoop, AgentRunResult, AgentRunSpec, AgentRunner
from .bus import InboundMessage, MessageBus, OutboundMessage
from .providers import LLMProvider, LLMResponse, MockProvider, ToolCallRequest
from .tools import AddTool, Tool, ToolRegistry, ToolResult

__version__ = "0.1.0"

__all__ = [
    "AddTool",
    "AgentLoop",
    "AgentRunResult",
    "AgentRunSpec",
    "AgentRunner",
    "InboundMessage",
    "LLMProvider",
    "LLMResponse",
    "MessageBus",
    "MockProvider",
    "OutboundMessage",
    "Tool",
    "ToolCallRequest",
    "ToolRegistry",
    "ToolResult",
]
