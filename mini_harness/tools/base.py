"""工具抽象与工具结果数据结构。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolResult:
    """一次工具执行的结构化结果。

    Args:
        content: 工具返回的文本；成功时为结果值，失败时为错误说明。
        is_error: 是否失败。Runner 不因该值为 True 而中断循环，而是把错误
            文本作为 role=tool 消息回传给模型。

    Returns:
        供 ToolRegistry 返回、AgentRunner 写入消息历史的结果对象。

    上下游:
        Tool.execute / ToolRegistry.execute 产生；AgentRunner._run_core
        读取 ``content``，以 ``tool_call_id`` 关联对应调用。
    """

    content: str
    is_error: bool = False

    @classmethod
    def error(cls, message: str) -> "ToolResult":
        """创建一个失败结果。

        Args:
            message: 面向模型和开发者的错误描述。

        Returns:
            ``is_error=True`` 的 ToolResult。
        """
        return cls(content=message, is_error=True)

    @property
    def success(self) -> bool:
        """成功时返回 True，便于测试和调用方读取。"""
        return not self.is_error


class Tool(ABC):
    """所有工具的最小抽象。

    子类通过类属性声明 ``name``、``description`` 和 JSON Schema 形式的
    ``parameters``，并实现异步 ``execute``。ToolRegistry 负责注册、查找、
    参数校验和异常兜底。
    """

    name: str = ""
    description: str = ""
    # JSON Schema：V0 只支持常见的顶层 object 参数校验。
    parameters: dict[str, Any] = {}

    def to_definition(self) -> dict[str, Any]:
        """转换为 OpenAI function 格式的工具定义。

        Returns:
            ``{"type": "function", "function": {...}}``。

        上下游:
            ToolRegistry.get_definitions 汇总所有定义并传给 Provider；
            AgentRunner 每轮 ``chat(messages, tools)`` 都会携带它。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate_arguments(self, arguments: Any) -> list[str]:
        """按 ``parameters`` JSON Schema 校验工具参数。

        Args:
            arguments: Provider 给出的参数，应为 JSON object（dict）。

        Returns:
            错误描述列表；空列表表示校验通过。本方法不抛异常，便于
            ToolRegistry 将校验失败统一转换为 ToolResult.error。

        上下游:
            ToolRegistry.execute 在真正调用 ``Tool.execute`` 前调用本方法；
            这样非法参数不会进入工具实现，也不会导致整个 Agent 崩溃。
        """
        if not isinstance(arguments, dict):
            return [f"参数必须是 JSON 对象，实际为 {type(arguments).__name__}"]

        schema = self.parameters
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        errors: list[str] = []

        for name in required:
            if name not in arguments:
                errors.append(f"缺少必填参数：{name}")

        for name, value in arguments.items():
            if name not in properties:
                if schema.get("additionalProperties", True) is False:
                    errors.append(f"不支持参数：{name}")
                continue

            expected_type = properties[name].get("type")
            if expected_type and not self._matches_type(value, expected_type):
                errors.append(
                    f"参数 {name} 类型错误：期望 {expected_type}，"
                    f"实际 {type(value).__name__}"
                )
                continue

            enum_values = properties[name].get("enum")
            if enum_values is not None and value not in enum_values:
                errors.append(f"参数 {name} 必须是 {enum_values} 之一")

        return errors

    @staticmethod
    def _matches_type(value: Any, expected_type: str) -> bool:
        """判断 Python 值是否符合 JSON Schema 标量/容器类型。

        特别注意：Python 中 ``bool`` 是 ``int`` 的子类，因此 integer/number
        校验必须显式排除 bool。
        """
        if expected_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "number":
            if isinstance(value, bool):
                return False
            # 任意精度 int 都合法；只有 float 需要额外排除 NaN/Infinity。
            if isinstance(value, int):
                return True
            return isinstance(value, float) and math.isfinite(value)
        if expected_type == "string":
            return isinstance(value, str)
        if expected_type == "boolean":
            return isinstance(value, bool)
        if expected_type == "array":
            return isinstance(value, list)
        if expected_type == "object":
            return isinstance(value, dict)
        return True

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具。

        Args:
            **kwargs: 已经过 ToolRegistry 校验的参数。

        Returns:
            ToolResult；业务失败应返回 ``ToolResult.error(...)``，而不是
            向 AgentRunner 抛异常。

        上下游:
            ToolRegistry.execute ``await`` 本方法；结果最终变成 role=tool
            消息，供 Provider 下一轮读取。
        """
        raise NotImplementedError
