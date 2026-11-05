"""离线演示使用的示例工具。"""

from .base import Tool, ToolResult


class AddTool(Tool):
    """计算两个数字之和的示例工具。"""

    name = "add"
    description = "计算两个数字的和"
    parameters = {
        "type": "object",
        "properties": {
            "a": {"type": "number", "description": "第一个加数"},
            "b": {"type": "number", "description": "第二个加数"},
        },
        "required": ["a", "b"],
        "additionalProperties": False,
    }

    async def execute(self, a: int | float, b: int | float) -> ToolResult:
        """执行加法并返回文本结果。

        Args:
            a: 第一个数字，由 ToolRegistry 校验为 number。
            b: 第二个数字，由 ToolRegistry 校验为 number。

        Returns:
            包含 ``str(a + b)`` 的 ToolResult。例如 a=2、b=3 时 content="5"。

        上下游:
            AgentRunner 通过 ToolRegistry.execute 调用；结果作为
            tool_call_id 对应的 role=tool 消息进入 messages。方法内部
            ``await`` 并非必须，但保持异步接口统一，便于未来接入 I/O 工具。
        """
        return ToolResult(content=str(a + b))
