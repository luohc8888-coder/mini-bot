"""集中维护的 Prompt 模板。

V2 只提供最小默认 System Prompt；不引入模板引擎、变量 DSL 或远程加载。
"""

from .defaults import DEFAULT_SYSTEM_PROMPT, build_default_system_prompt

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "build_default_system_prompt",
]
