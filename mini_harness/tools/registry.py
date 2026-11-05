"""工具注册、参数校验与受限执行。"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from .base import Tool, ToolResult


class ToolRegistry:
    """工具注册、Schema 导出、参数校验与执行入口。

    当前逐个执行工具；不实现 MCP、权限控制、并发执行或远程工具。
    """

    def __init__(
        self, *, timeout_seconds: float = 130.0, max_output_chars: int = 32_000
    ) -> None:
        """初始化空注册表。"""
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        if max_output_chars < 256:
            raise ValueError("max_output_chars must be >= 256")
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。

        Args:
            tool: Tool 实例，必须有非空 name。

        Returns:
            None。

        上下游:
            CLI 在启动时注册 AddTool；AgentRunner 通过 get_definitions
            和 execute 使用注册表。重复 name 会覆盖旧工具
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
        """统一限制成功与失败结果的输出大小；取消信号仍向上传播。"""
        result = await self._execute(name, arguments)
        if len(result.content) > self.max_output_chars:
            marker = "\n[... tool output truncated by registry ...]\n"
            remaining = self.max_output_chars - len(marker)
            head = remaining * 3 // 4
            return ToolResult(
                result.content[:head] + marker + result.content[-(remaining - head):],
                is_error=result.is_error,
            )
        return result

    async def _execute(self, name: str, arguments: dict[str, Any] | None) -> ToolResult:
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
            result = await asyncio.wait_for(
                tool.execute(**normalized_arguments), timeout=self.timeout_seconds
            )
        except TimeoutError:
            return ToolResult.error(
                f"工具 '{name}' 执行超时（{self.timeout_seconds:g}s）"
            )
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
        if not isinstance(result.content, str):
            return ToolResult.error(f"工具 '{name}' 返回了非文本 content")
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
