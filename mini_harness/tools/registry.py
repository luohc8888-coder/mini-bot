"""工具注册表与 V0 唯一的 add 工具。"""

from __future__ import annotations

from typing import Any

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


class ToolRegistry:
    """工具注册、Schema 导出、参数校验与执行入口。

    V0 仅支持顺序执行；不实现 MCP、权限控制、并发执行或远程工具。
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。

        Args:
            tool: Tool 实例，必须有非空 name。

        Returns:
            None。

        上下游:
            main.py 在启动时注册 AddTool；AgentRunner 通过 get_definitions
            和 execute 使用注册表。重复 name 会覆盖旧工具，与 Nanobot 的
            简单注册行为一致。
        """
        if not isinstance(tool.name, str) or not tool.name:
            raise ValueError("工具必须有非空 name")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """按名称查找工具。

        Args:
            name: 工具名。

        Returns:
            找到时返回 Tool，否则返回 None。

        上下游:
            AgentRunner / ToolRegistry.execute 通过它解析 Provider 返回的
            tool_call.name。
        """
        return self._tools.get(name)

    def get_definitions(self) -> list[dict[str, Any]]:
        """导出全部工具定义。

        Returns:
            OpenAI function 格式的定义列表，顺序为注册顺序。

        上下游:
            AgentRunner._run_core 在迭代循环开始前调用一次，并把结果复用于
            每轮 Provider.chat。真实 Provider 会把定义放入请求，让模型知道
            可用工具。
        """
        return [tool.to_definition() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any] | None) -> ToolResult:
        """校验并执行一次工具调用。

        Args:
            name: Provider 请求的工具名。
            arguments: Provider 给出的参数；None 视为空对象。

        Returns:
            ToolResult。未知工具、非法参数、工具内部异常都会转换成
            ``is_error=True`` 的错误结果，而不是把异常抛给 AgentRunner。

        上下游:
            AgentRunner._run_core 对每个 ToolCallRequest ``await`` 本方法。
            所有失败也必须返回结果，才能保证每个 assistant tool_call 都有
            一条 role=tool 消息与之配对。
        """
        tool = self.get(name)
        if tool is None:
            available = ", ".join(self._tools) or "(无)"
            return ToolResult.error(
                f"未找到工具 '{name}'。可用工具：{available}"
            )

        normalized_arguments: dict[str, Any] = {} if arguments is None else arguments
        validation_errors = tool.validate_arguments(normalized_arguments)
        if validation_errors:
            return ToolResult.error(
                f"工具 '{name}' 参数校验失败：" + "；".join(validation_errors)
            )

        try:
            # await 工具协程；这样文件/网络类工具未来不会阻塞事件循环。
            result = await tool.execute(**normalized_arguments)
        except TypeError as exc:
            # 参数与 execute 签名不匹配（通常说明 Schema 声明不准确）。
            return ToolResult.error(f"工具 '{name}' 参数不匹配：{exc}")
        except Exception as exc:
            # 工具内部错误只转成错误消息，不打断 AgentLoop。
            return ToolResult.error(
                f"工具 '{name}' 执行失败：{type(exc).__name__}: {exc}"
            )

        if not isinstance(result, ToolResult):
            return ToolResult.error(
                f"工具 '{name}' 返回了非法结果类型：{type(result).__name__}"
            )
        return result

    @property
    def tool_names(self) -> list[str]:
        """返回已注册工具名列表（注册顺序）。"""
        return list(self._tools)

    def __len__(self) -> int:
        """返回已注册工具数量。"""
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """支持 ``name in registry`` 的成员判断。"""
        return self.get(name) is not None
