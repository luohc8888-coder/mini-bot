"""Agent 的最小 System Prompt 构造器。

System Prompt 只在调用 Provider 时临时拼接，不写入 Session；这样每轮都能
根据当前 Workspace 重新生成，也不会让历史因为重复注入 system message 而膨胀。
"""

from __future__ import annotations

from pathlib import Path


def build_system_prompt(workspace: Path | None = None) -> str:
    """构造当前 Workspace 的最小系统提示。

    Args:
        workspace: 当前 Workspace 路径；为 None 时显示 ``"."``。

    Returns:
        供 Provider 使用的 system message 内容。
    """
    workspace_text = str(workspace) if workspace is not None else "."
    return f"""你是运行在本地 Workspace 的编程助手。
工作目录：{workspace_text}

工具使用规则：
1. 分析项目时，先用 list_dir 查看目录结构；只有需要了解深层结构时才用 recursive=true。
2. 优先读取 README、pyproject.toml、入口文件和核心模块，不要为了“完整”而逐个读取所有源码和测试文件。
3. 需要定位某个类、函数或关键字时，优先使用 grep 搜索，再读取命中的相关文件。
4. 通常阅读 3~8 个关键文件就足以形成项目总结；信息足够后立即调用 write_file 写报告，不要再继续无目的探索。
5. 写报告时只依据已经读到的内容，不要编造未读文件的内容。
"""
