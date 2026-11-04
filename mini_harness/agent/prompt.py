"""兼容 V1 的最小 System Prompt 构造入口。

实际模板已经集中到 :mod:`mini_harness.prompts.defaults`。保留本模块是为了
不破坏 V1 的 ``from mini_harness.agent import build_system_prompt`` 导入
路径；V2 的正式上下文构建入口是 :class:`mini_harness.agent.context.ContextBuilder`。
"""

from __future__ import annotations

from pathlib import Path

from mini_harness.prompts.defaults import (
    DEFAULT_SYSTEM_PROMPT,
    build_default_system_prompt,
)


def build_system_prompt(workspace: Path | None = None) -> str:
    """构造当前 Workspace 的最小系统提示（V1 兼容函数）。

    Args:
        workspace: 当前 Workspace 路径；为 None 时显示 ``"."``。

    Returns:
        不含 AGENTS.md 的默认 System Prompt。

    Notes:
        V2 的 ``ContextBuilder.build_system_prompt()`` 会在此基础上追加
        Workspace 根目录的 AGENTS.md。两者共用同一份模板，避免出现两份
        不一致的默认提示词。
    """
    return build_default_system_prompt(workspace)


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "build_default_system_prompt",
    "build_system_prompt",
]
