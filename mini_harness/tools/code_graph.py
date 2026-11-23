"""面向 Agent 的只读 CodeGraph 查询工具（查询符号定义、callers、callees 与子节点）。"""

from __future__ import annotations

from typing import Any

from mini_harness.explorer.codegraph import CodeGraph
from mini_harness.explorer.models import SymbolRecord

from .base import Tool, ToolResult


class QueryCodeGraphTool(Tool):
    """查询本地 CodeGraph 中指定符号的精确位置、上游调用者（callers）与下游被调用者（callees）。"""

    name = "query_code_graph"
    description = (
        "查询 CodeGraph 中某个符号的精确文件行号、签名、直接上游调用者（callers）、"
        "下游被调用者（callees）及子符号（支持跨 Python/TS/JS/Java 与前后端 API 桥接边）。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "symbol_id、qualified_name 或符号短名（例如 'function:mini_harness.web.app.create_app.chat_source' 或 'fetchChatReply'）。",
            },
            "max_neighbors": {
                "type": "integer",
                "description": "callers/callees/children 各自最多返回条数，默认 12。",
                "minimum": 1,
                "maximum": 30,
                "default": 12,
            },
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }

    def __init__(self, graph: CodeGraph) -> None:
        self._graph = graph

    async def execute(
        self,
        symbol: str,
        max_neighbors: int = 12,
    ) -> ToolResult:
        query = symbol.strip()
        if not query:
            return ToolResult.error("symbol 不能为空。")

        matches = _resolve_symbols(self._graph, query)
        if not matches:
            return ToolResult(
                content=f"CodeGraph 中未找到符号 {query!r}，可先调用 search_code 检索。"
            )

        target = matches[0]
        limit = max(1, min(int(max_neighbors), 30))
        lines: list[str] = [
            f"符号：[{target.kind}] {target.qualified_name}",
            f"symbol_id: {target.id}",
            f"位置：{target.file_path}:{target.start_line}-{target.end_line}（{target.language}）",
        ]
        if target.signature:
            lines.append(f"签名：{target.signature}")
        if target.docstring:
            first_doc = target.docstring.strip().splitlines()[0].strip()
            if first_doc:
                lines.append(f"文档：{first_doc[:160]}")

        callers = self._graph.get_callers(target.id)[:limit]
        callees = self._graph.get_callees(target.id)[:limit]
        children = self._graph.get_children(target.id)[:limit]

        if callers:
            lines.append(f"\n上游调用者 callers ({len(callers)}):")
            for item in callers:
                lines.append(
                    f"  - [{item.kind}] {item.qualified_name} "
                    f"({item.file_path}:{item.start_line}-{item.end_line})"
                )
        else:
            lines.append("\n上游调用者 callers: (无静态记录)")

        if callees:
            lines.append(f"\n下游被调用者 callees ({len(callees)}):")
            for item in callees:
                lines.append(
                    f"  - [{item.kind}] {item.qualified_name} "
                    f"({item.file_path}:{item.start_line}-{item.end_line})"
                )
        else:
            lines.append("\n下游被调用者 callees: (无静态记录)")

        if children:
            lines.append(f"\n直接子符号 children ({len(children)}):")
            for item in children:
                lines.append(
                    f"  - [{item.kind}] {item.name} "
                    f"({item.file_path}:{item.start_line}-{item.end_line})"
                )

        if len(matches) > 1:
            others = ", ".join(
                f"{m.qualified_name} ({m.file_path}:{m.start_line})"
                for m in matches[1:4]
            )
            lines.append(f"\n其他同名候选：{others}")

        return ToolResult(content="\n".join(lines))


def _resolve_symbols(graph: CodeGraph, query: str) -> list[SymbolRecord]:
    direct = graph.get_node(query)
    if direct is not None:
        return [direct]

    q_lower = query.lower()
    exact_qname: list[SymbolRecord] = []
    exact_name: list[SymbolRecord] = []
    suffix_matches: list[SymbolRecord] = []

    for node in graph.nodes.values():
        if node.qualified_name == query:
            exact_qname.append(node)
        elif node.name == query:
            exact_name.append(node)
        elif node.qualified_name.lower().endswith(f".{q_lower}"):
            suffix_matches.append(node)

    return exact_qname or exact_name or suffix_matches


__all__ = ["QueryCodeGraphTool"]
