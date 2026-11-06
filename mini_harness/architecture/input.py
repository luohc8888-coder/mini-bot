"""Phase 4.2：从 CodeGraph 构建语义架构分析输入。

这一层只做确定性的结构化整理，不调用 LLM。它把完整 CodeGraph 压缩成
未来 ArchitectureAnalyzer 可以消费的输入包：

- 入口候选与关键符号（带真实 SymbolIndex ID）；
- 模块清单；
- 静态 import / call 关系（有数量上限）；
- 入口/关键符号的有限源码窗口（按需读取，绝不倾倒整个项目）；
- 源码引用协议和本次输入的限制参数。

真正的语义组件划分、执行链推断和 token 预算在 Phase 4.3 实现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mini_harness.explorer import SCHEMA_VERSION, CodeGraph, GraphEdge, ProjectIndex, SymbolRecord

from .source_refs import SOURCE_REFERENCE_PROTOCOL, SymbolIndex

ARCHITECTURE_INPUT_SCHEMA_VERSION = 1

_DEFAULT_MAX_MODULES = 80
_DEFAULT_MAX_KEY_SYMBOLS = 80
_DEFAULT_MAX_IMPORT_RELATIONS = 120
_DEFAULT_MAX_CALL_RELATIONS = 220
_DEFAULT_MAX_ENTRY_POINTS = 12
_DEFAULT_MAX_SOURCE_EXCERPTS = 10
_DEFAULT_MAX_EXCERPT_LINES = 80
_DEFAULT_MAX_EXCERPT_CHARS = 12_000
_DEFAULT_MAX_TOTAL_EXCERPT_CHARS = 48_000

_ENTRY_NAMES = frozenset(
    {"main", "run", "start", "serve", "server", "execute", "cli", "app", "loop"}
)
_ENTRY_FILE_STEMS = frozenset({"main", "__main__", "cli", "app", "server"})
_COMPONENT_HINTS = frozenset(
    {
        "AgentLoop",
        "AgentRunner",
        "MessageBus",
        "ToolRegistry",
        "ContextGovernor",
        "SessionManager",
        "LLMProvider",
    }
)


@dataclass(frozen=True, slots=True)
class ArchitectureSymbolSummary:
    """给分析器的紧凑符号摘要，不包含源码正文。"""

    id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None
    parent_id: str | None = None
    caller_count: int = 0
    callee_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "qualifiedName": self.qualified_name,
            "filePath": self.file_path,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "signature": self.signature,
            "docstring": self.docstring,
            "parentId": self.parent_id,
            "callerCount": self.caller_count,
            "calleeCount": self.callee_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectureSymbolSummary:
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            name=str(data["name"]),
            qualified_name=str(data.get("qualifiedName", data["name"])),
            file_path=str(data.get("filePath", "")),
            start_line=int(data.get("startLine", 1)),
            end_line=int(data.get("endLine", data.get("startLine", 1))),
            signature=data.get("signature"),
            docstring=data.get("docstring"),
            parent_id=data.get("parentId"),
            caller_count=int(data.get("callerCount", 0)),
            callee_count=int(data.get("calleeCount", 0)),
        )


@dataclass(frozen=True, slots=True)
class ArchitectureEntryPoint:
    """一个可能的项目入口及判定信号。"""

    symbol: ArchitectureSymbolSummary
    reasons: list[str] = field(default_factory=list)
    score: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.to_dict(),
            "reasons": list(self.reasons),
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureRelationSummary:
    """从 CodeGraph 抽取的一条静态关系。"""

    id: str
    source: str
    target: str
    kind: str
    confidence: str
    line: int | None = None
    detail: str | None = None
    occurrences: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "confidence": self.confidence,
            "line": self.line,
            "detail": self.detail,
            "occurrences": self.occurrences,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectureRelationSummary:
        return cls(
            id=str(data["id"]),
            source=str(data["source"]),
            target=str(data["target"]),
            kind=str(data["kind"]),
            confidence=str(data.get("confidence", "static")),
            line=int(data["line"]) if data.get("line") is not None else None,
            detail=data.get("detail"),
            occurrences=int(data.get("occurrences", 1)),
        )

    @classmethod
    def from_edge(cls, edge: GraphEdge) -> ArchitectureRelationSummary:
        return cls(
            id=edge.id,
            source=edge.source,
            target=edge.target,
            kind=edge.kind,
            confidence=edge.confidence,
            line=edge.line,
            detail=edge.detail,
            occurrences=edge.occurrences,
        )


@dataclass(frozen=True, slots=True)
class ArchitectureSourceExcerpt:
    """一段按符号范围截取的源码窗口。"""

    symbol_id: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    text: str
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbolId": self.symbol_id,
            "qualifiedName": self.qualified_name,
            "filePath": self.file_path,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "text": self.text,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureAnalysisInput:
    """未来 LLM 架构分析的结构化输入包。"""

    workspace_root: str
    workspace_id: str
    project_name: str
    generated_at: str
    graph_stats: dict[str, int]
    entry_points: list[ArchitectureEntryPoint]
    modules: list[ArchitectureSymbolSummary]
    key_symbols: list[ArchitectureSymbolSummary]
    imports: list[ArchitectureRelationSummary]
    calls: list[ArchitectureRelationSummary]
    source_excerpts: list[ArchitectureSourceExcerpt]
    unresolved_summary: dict[str, int]
    limits: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    schema_version: int = ARCHITECTURE_INPUT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "workspaceRoot": self.workspace_root,
            "workspaceId": self.workspace_id,
            "project": {
                "name": self.project_name,
                "root": self.workspace_root,
            },
            "generatedAt": self.generated_at,
            "graphStats": dict(self.graph_stats),
            "stats": dict(self.graph_stats),
            "entryPoints": [item.to_dict() for item in self.entry_points],
            "modules": [item.to_dict() for item in self.modules],
            "keySymbols": [item.to_dict() for item in self.key_symbols],
            "imports": [item.to_dict() for item in self.imports],
            "calls": [item.to_dict() for item in self.calls],
            "sourceExcerpts": [item.to_dict() for item in self.source_excerpts],
            "unresolvedSummary": dict(self.unresolved_summary),
            "limits": dict(self.limits),
            "warnings": list(self.warnings),
            "sourceReferenceProtocol": dict(SOURCE_REFERENCE_PROTOCOL),
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class ArchitectureInputBuilder:
    """把 CodeGraph 转换为可验证、受大小限制的语义分析输入。"""

    def __init__(
        self,
        index: ProjectIndex | CodeGraph,
        graph: CodeGraph | None = None,
        *,
        max_modules: int = _DEFAULT_MAX_MODULES,
        max_key_symbols: int = _DEFAULT_MAX_KEY_SYMBOLS,
        max_import_relations: int = _DEFAULT_MAX_IMPORT_RELATIONS,
        max_call_relations: int = _DEFAULT_MAX_CALL_RELATIONS,
        max_entry_points: int = _DEFAULT_MAX_ENTRY_POINTS,
        max_source_excerpts: int = _DEFAULT_MAX_SOURCE_EXCERPTS,
        max_excerpt_lines: int = _DEFAULT_MAX_EXCERPT_LINES,
        max_excerpt_chars: int = _DEFAULT_MAX_EXCERPT_CHARS,
        max_total_excerpt_chars: int = _DEFAULT_MAX_TOTAL_EXCERPT_CHARS,
    ) -> None:
        if graph is None:
            if not isinstance(index, CodeGraph):
                raise TypeError("未提供 graph 时，第一个参数必须是 CodeGraph")
            self.graph = index
            self.index = ProjectIndex(
                schema_version=SCHEMA_VERSION,
                workspace_root=index.workspace_root,
                workspace_id=index.workspace_id,
                generated_at="",
                symbols=list(index.nodes.values()),
            )
        else:
            self.index = index
            self.graph = graph
        self.symbol_index = SymbolIndex.from_code_graph(self.graph)
        self.max_modules = max(0, max_modules)
        self.max_key_symbols = max(0, max_key_symbols)
        self.max_import_relations = max(0, max_import_relations)
        self.max_call_relations = max(0, max_call_relations)
        self.max_entry_points = max(0, max_entry_points)
        self.max_source_excerpts = max(0, max_source_excerpts)
        self.max_excerpt_lines = max(1, max_excerpt_lines)
        self.max_excerpt_chars = max(1, max_excerpt_chars)
        self.max_total_excerpt_chars = max(0, max_total_excerpt_chars)

    @classmethod
    def from_graph(cls, graph: CodeGraph, **kwargs: Any) -> ArchitectureAnalysisInput:
        """仅给 CodeGraph 时也能构建输入（generatedAt 为空）。"""
        return cls(graph, **kwargs).build()

    def build(self) -> ArchitectureAnalysisInput:
        """构建完整输入包；过程纯确定性，不发起任何模型调用。"""
        warnings: list[str] = []
        modules = self._select_modules()
        key_symbols = self._select_key_symbols()
        entry_points = self._select_entry_points(key_symbols)
        imports = self._select_relations("imports", self.max_import_relations)
        calls = self._select_relations("calls", self.max_call_relations)
        source_excerpts = self._build_source_excerpts(
            self._excerpt_candidates(entry_points, key_symbols),
            warnings,
        )
        unresolved_summary = self._unresolved_summary()
        return ArchitectureAnalysisInput(
            workspace_root=self.graph.workspace_root,
            workspace_id=self.graph.workspace_id,
            project_name=Path(self.graph.workspace_root).name,
            generated_at=self.index.generated_at,
            graph_stats=dict(self.graph.stats),
            entry_points=entry_points,
            modules=modules,
            key_symbols=key_symbols,
            imports=imports,
            calls=calls,
            source_excerpts=source_excerpts,
            unresolved_summary=unresolved_summary,
            limits=self._limits(),
            warnings=warnings,
        )

    def _summary(self, symbol: SymbolRecord) -> ArchitectureSymbolSummary:
        return ArchitectureSymbolSummary(
            id=symbol.id,
            kind=symbol.kind,
            name=symbol.name,
            qualified_name=symbol.qualified_name,
            file_path=symbol.file_path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            signature=symbol.signature,
            docstring=symbol.docstring,
            parent_id=symbol.parent_id,
            caller_count=len(self.graph.get_incoming(symbol.id, kind="calls")),
            callee_count=len(self.graph.get_outgoing(symbol.id, kind="calls")),
        )

    def _select_modules(self) -> list[ArchitectureSymbolSummary]:
        modules = [
            symbol
            for symbol in self.graph.node_list
            if symbol.kind == "module"
        ]
        modules.sort(key=lambda item: (item.file_path, item.start_line))
        return [self._summary(symbol) for symbol in modules[: self.max_modules]]

    def _select_key_symbols(self) -> list[ArchitectureSymbolSummary]:
        candidates = [
            symbol
            for symbol in self.graph.node_list
            if symbol.kind in {"class", "function", "method"}
        ]
        ranked = sorted(
            candidates,
            key=lambda symbol: (
                -self._key_score(symbol),
                symbol.file_path,
                symbol.start_line,
                symbol.qualified_name,
            ),
        )
        return [self._summary(symbol) for symbol in ranked[: self.max_key_symbols]]

    def _key_score(self, symbol: SymbolRecord) -> int:
        score = 0
        if symbol.kind == "class":
            score += 30
        elif symbol.kind == "function":
            score += 18
        else:
            score += 8
        if symbol.name in _COMPONENT_HINTS:
            score += 120
        if symbol.name.startswith("_"):
            score -= 12
        score += len(self.graph.get_incoming(symbol.id, kind="calls")) * 4
        score += len(self.graph.get_outgoing(symbol.id, kind="calls")) * 2
        return score

    def _select_entry_points(
        self,
        key_symbols: list[ArchitectureSymbolSummary],
    ) -> list[ArchitectureEntryPoint]:
        candidates: list[ArchitectureEntryPoint] = []
        for symbol in self.graph.node_list:
            if symbol.kind not in {"class", "function", "method"}:
                continue
            score, reasons = self._entry_score(symbol)
            if score <= 0 or not reasons:
                continue
            candidates.append(
                ArchitectureEntryPoint(
                    symbol=self._summary(symbol),
                    reasons=reasons,
                    score=score,
                )
            )
        candidates.sort(
            key=lambda item: (
                -item.score,
                item.symbol.file_path,
                item.symbol.start_line,
            )
        )
        selected = candidates[: self.max_entry_points]
        if selected:
            return selected
        # 极端情况下项目没有命名入口，退回连接度最高的关键符号，
        # 但明确标注是推导出的候选入口。
        return [
            ArchitectureEntryPoint(
                symbol=item,
                reasons=["连接度最高的符号，未发现显式入口命名"],
                score=0,
            )
            for item in key_symbols[: min(5, self.max_entry_points)]
        ]

    def _entry_score(self, symbol: SymbolRecord) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        name = symbol.name.lower()
        file_stem = Path(symbol.file_path).stem.lower()
        path_parts = {part.lower() for part in Path(symbol.file_path).parts}

        if name in _ENTRY_NAMES:
            score += 60
            reasons.append(f"入口命名：{symbol.name}")
        if symbol.kind == "function" and name == "main":
            score += 100
            reasons.append("顶层 main 函数")
        if symbol.kind == "method" and name in {"run", "serve", "start", "loop"}:
            score += 45
            reasons.append(f"生命周期方法：{symbol.name}")
        if file_stem in _ENTRY_FILE_STEMS:
            score += 30
            reasons.append(f"入口文件：{Path(symbol.file_path).name}")
        if "cli" in path_parts:
            score += 20
            reasons.append("CLI 目录")
        score += len(self.graph.get_incoming(symbol.id, kind="calls")) * 2
        return score, reasons

    def _select_relations(self, kind: str, limit: int) -> list[ArchitectureRelationSummary]:
        edges = [
            edge
            for edge in self.graph.edges
            if edge.kind == kind
            and edge.source in self.graph.nodes
            and edge.target in self.graph.nodes
        ]
        key_ids = {item.id for item in self._select_key_symbols()}
        edges.sort(
            key=lambda edge: (
                -(
                    (4 if edge.source in key_ids else 0)
                    + (4 if edge.target in key_ids else 0)
                    + edge.occurrences
                ),
                edge.source,
                edge.target,
                edge.id,
            ),
        )
        return [
            ArchitectureRelationSummary.from_edge(edge)
            for edge in edges[:limit]
        ]

    def _excerpt_candidates(
        self,
        entry_points: list[ArchitectureEntryPoint],
        key_symbols: list[ArchitectureSymbolSummary],
    ) -> list[ArchitectureSymbolSummary]:
        """优先读取关键逻辑组件，再读取入口函数，最后补充其他关键符号。"""
        ordered: list[ArchitectureSymbolSummary] = []
        seen: set[str] = set()

        def add(symbol: ArchitectureSymbolSummary) -> None:
            if symbol.id not in seen:
                seen.add(symbol.id)
                ordered.append(symbol)

        for symbol in key_symbols:
            if symbol.name in _COMPONENT_HINTS and symbol.kind == "class":
                add(symbol)
        for entry in entry_points:
            add(entry.symbol)
        for symbol in key_symbols:
            add(symbol)
        return ordered

    def _build_source_excerpts(
        self,
        symbols: list[ArchitectureSymbolSummary],
        warnings: list[str],
    ) -> list[ArchitectureSourceExcerpt]:
        root = Path(self.index.workspace_root).resolve()
        excerpts: list[ArchitectureSourceExcerpt] = []
        seen: set[tuple[str, int, int]] = set()
        total_chars = 0
        for symbol in symbols:
            if len(excerpts) >= self.max_source_excerpts:
                break
            if total_chars >= self.max_total_excerpt_chars:
                break
            if symbol.kind == "module":
                continue
            key = (symbol.file_path, symbol.start_line, symbol.end_line)
            if key in seen:
                continue
            seen.add(key)
            path = (root / symbol.file_path).resolve()
            if not path.is_relative_to(root):
                warnings.append(f"跳过 Workspace 外路径：{symbol.file_path}")
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                warnings.append(f"无法读取源码 {symbol.file_path}: {exc}")
                continue
            start = max(1, symbol.start_line)
            end = min(len(lines), symbol.end_line, start + self.max_excerpt_lines - 1)
            if end < start:
                warnings.append(f"源码范围为空：{symbol.file_path}:{symbol.start_line}")
                continue
            text = "\n".join(lines[start - 1 : end])
            remaining_chars = self.max_total_excerpt_chars - total_chars
            char_limit = min(self.max_excerpt_chars, remaining_chars)
            truncated = end < symbol.end_line or len(text) > char_limit
            if len(text) > char_limit:
                text = text[:char_limit].rstrip()
            total_chars += len(text)
            excerpts.append(
                ArchitectureSourceExcerpt(
                    symbol_id=symbol.id,
                    qualified_name=symbol.qualified_name,
                    file_path=symbol.file_path,
                    start_line=start,
                    end_line=start + text.count("\n"),
                    text=text,
                    truncated=truncated,
                )
            )
        return excerpts

    def _unresolved_summary(self) -> dict[str, int]:
        imports = sum(1 for item in self.graph.unresolved if item.kind == "imports")
        calls = sum(1 for item in self.graph.unresolved if item.kind == "calls")
        return {
            "total": len(self.graph.unresolved),
            "imports": imports,
            "calls": calls,
        }

    def _limits(self) -> dict[str, int]:
        return {
            "maxModules": self.max_modules,
            "maxKeySymbols": self.max_key_symbols,
            "maxImportRelations": self.max_import_relations,
            "maxCallRelations": self.max_call_relations,
            "maxEntryPoints": self.max_entry_points,
            "maxSourceExcerpts": self.max_source_excerpts,
            "maxExcerptLines": self.max_excerpt_lines,
            "maxExcerptChars": self.max_excerpt_chars,
            "maxTotalExcerptChars": self.max_total_excerpt_chars,
        }


ArchitectureAnalysisInputBuilder = ArchitectureInputBuilder


def build_architecture_input(
    index: ProjectIndex | CodeGraph,
    graph: CodeGraph | None = None,
    **kwargs: Any,
) -> ArchitectureAnalysisInput:
    """便捷入口：可从 ProjectIndex+CodeGraph 或仅从 CodeGraph 构建。"""
    return ArchitectureInputBuilder(index, graph, **kwargs).build()


__all__ = [
    "ARCHITECTURE_INPUT_SCHEMA_VERSION",
    "ArchitectureAnalysisInput",
    "ArchitectureAnalysisInputBuilder",
    "ArchitectureEntryPoint",
    "ArchitectureInputBuilder",
    "ArchitectureRelationSummary",
    "ArchitectureSourceExcerpt",
    "ArchitectureSymbolSummary",
    "build_architecture_input",
]
