"""面向 Agent 的混合代码检索工具（SQLite FTS5 + CodeGraph RRF）。

相比纯正则 ``grep``，``search_code`` 利用增量维护的 SQLite 索引同时检索
符号名（含驼峰/下划线展开）、签名、docstring、文件路径与源码片段，并结合
CodeGraph 调用中心性排序，返回带行号和直接 callers/callees 的结构化定位结果。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mini_harness.explorer.sqlite_store import CodeSearchHit, SQLiteIndexStore
from mini_harness.workspace import Workspace, WorkspacePathError

from .base import Tool, ToolResult


class SearchCodeTool(Tool):
    """基于本地 SQLite (WAL + FTS5) 与 CodeGraph 的混合代码检索工具。"""

    name = "search_code"
    description = (
        "【代码定位首选】基于 SQLite FTS5 与 CodeGraph 的混合代码检索。"
        "当需要查找某功能实现、中文概念、类/函数/方法位置及上下游调用链（callers/callees）时，"
        "优先调用此工具直接获取精确文件路径与行号范围，再按行号 read_file（精确正则文本匹配才用 grep）。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "概念、符号名或关键词组合。",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回条数（1-25，默认 8）。",
                "minimum": 1,
                "maximum": 25,
                "default": 8,
            },
            "kind": {
                "type": "string",
                "enum": ["module", "class", "function", "method"],
                "description": "按符号类型过滤。",
            },
            "path_prefix": {
                "type": "string",
                "description": "限定 Workspace 内的相对路径前缀。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Workspace,
        *,
        index_store: SQLiteIndexStore | None = None,
        base_dir: str | Path | None = None,
    ) -> None:
        self.workspace = workspace
        self._store = index_store or SQLiteIndexStore(workspace, base_dir=base_dir)

    async def execute(
        self,
        query: str,
        limit: int = 8,
        kind: str | None = None,
        path_prefix: str | None = None,
    ) -> ToolResult:
        cleaned_query = query.strip()
        if not cleaned_query:
            return ToolResult.error("query 不能为空。")

        normalized_prefix: str | None = None
        if path_prefix is not None and path_prefix.strip():
            raw_prefix = path_prefix.strip()
            try:
                resolved = self.workspace.resolve_path(raw_prefix)
            except WorkspacePathError as exc:
                return ToolResult.error(str(exc))
            rel = resolved.relative_to(self.workspace.path).as_posix()
            normalized_prefix = None if rel == "." else rel

        try:
            hits = await asyncio.to_thread(
                self._store.search,
                cleaned_query,
                limit=limit,
                kind=kind,
                path_prefix=normalized_prefix,
                auto_sync=True,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"代码检索失败：{exc}")

        if not hits:
            return ToolResult(
                content=f"未找到与 {cleaned_query!r} 匹配的代码符号。可尝试更换关键词或改用 grep。"
            )

        return ToolResult(content=_format_hits(cleaned_query, hits))


def _format_hits(query: str, hits: list[CodeSearchHit]) -> str:
    lines: list[str] = [f"共命中 {len(hits)} 个相关符号（query: {query}）："]
    for idx, hit in enumerate(hits, start=1):
        location = f"{hit.file_path}:{hit.start_line}-{hit.end_line}"
        lines.append(
            f"{idx}. [{hit.kind}] {hit.qualified_name} ({location}) "
            f"[score={hit.score:.3f}, in={hit.in_degree}, out={hit.out_degree}]"
        )
        if hit.signature:
            lines.append(f"   signature: {hit.signature}")
        if hit.docstring:
            first_doc = hit.docstring.strip().splitlines()[0].strip()
            if len(first_doc) > 140:
                first_doc = first_doc[:139] + "…"
            if first_doc:
                lines.append(f"   doc: {first_doc}")
        rel_parts: list[str] = []
        if hit.callers:
            rel_parts.append(f"callers: {', '.join(hit.callers)}")
        if hit.callees:
            rel_parts.append(f"callees: {', '.join(hit.callees)}")
        if rel_parts:
            lines.append(f"   {' | '.join(rel_parts)}")
    return "\n".join(lines)


__all__ = ["SearchCodeTool"]
