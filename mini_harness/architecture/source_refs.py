"""Phase 4.2：SymbolIndex 与源码引用校验协议。

AI 生成的架构节点可以没有源码关联（例如抽象的 User/Response 阶段），
但只要声明了源码引用，就必须能解析到当前 Workspace 的真实符号。
无法通过 SymbolIndex 解析的引用会被视为幻觉并拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from mini_harness.explorer import CodeGraph, ProjectIndex, SymbolRecord

from .models import (
    ARCHITECTURE_SCHEMA_VERSION,
    SOURCE_REFERENCE_PROTOCOL_VERSION,
    ArchitectureEdge,
    ArchitectureGraph,
    ArchitectureNode,
    ArchitectureReferenceError,
    ArchitectureSourceRef,
    ArchitectureView,
)

SOURCE_REFERENCE_PROTOCOL: dict[str, Any] = {
    "version": SOURCE_REFERENCE_PROTOCOL_VERSION,
    "architectureSchemaVersion": ARCHITECTURE_SCHEMA_VERSION,
    "referenceFields": [
        "symbolId",
        "filePath",
        "startLine",
        "endLine",
        "qualifiedName",
        "verified",
    ],
    "required": "每个 code reference 至少提供 symbolId、qualifiedName 或精确的 filePath+startLine",
    "rules": [
        "symbolId 必须存在于当前 Workspace 的 SymbolIndex",
        "filePath / startLine / endLine 如提供，必须落在对应符号范围内",
        "解析成功时由后端回填 symbolId、filePath、startLine、endLine 与 verified=true",
        "无法静态确认的架构关系必须使用 confidence=inferred，并附带 evidence 或说明",
        "不允许为了填充架构图而编造不存在的源码符号",
    ],
}


class SymbolIndex:
    """基于 ProjectIndex / CodeGraph 的轻量符号查询索引。"""

    def __init__(self, symbols: Iterable[SymbolRecord]):
        self._symbols = tuple(symbols)
        self._by_id: dict[str, SymbolRecord] = {}
        self._by_qualified: dict[str, list[SymbolRecord]] = {}
        self._by_file: dict[str, list[SymbolRecord]] = {}

        for symbol in self._symbols:
            self._by_id[symbol.id] = symbol
            self._by_qualified.setdefault(symbol.qualified_name, []).append(symbol)
            self._by_file.setdefault(symbol.file_path, []).append(symbol)

        for file_symbols in self._by_file.values():
            file_symbols.sort(key=lambda item: (item.start_line, item.end_line))

    @classmethod
    def from_project_index(cls, index: ProjectIndex) -> SymbolIndex:
        return cls(index.symbols)

    @classmethod
    def from_code_graph(cls, graph: CodeGraph) -> SymbolIndex:
        return cls(graph.nodes.values())

    def __len__(self) -> int:
        return len(self._symbols)

    @property
    def symbols(self) -> tuple[SymbolRecord, ...]:
        return self._symbols

    def get(self, symbol_id: str) -> SymbolRecord | None:
        return self._by_id.get(symbol_id)

    def find_by_qualified_name(self, qualified_name: str) -> list[SymbolRecord]:
        return list(self._by_qualified.get(qualified_name, []))

    def find_at(self, file_path: str, line: int) -> SymbolRecord | None:
        """返回包含指定文件行号的最具体符号。"""
        candidates = [
            symbol
            for symbol in self._by_file.get(file_path, [])
            if symbol.start_line <= line <= symbol.end_line
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.end_line - item.start_line, item.start_line))
        return candidates[0]


class SourceRefProtocol:
    """把 LLM 给出的源码引用标准化并验证到 SymbolIndex。"""

    def __init__(self, symbol_index: SymbolIndex):
        self.symbol_index = symbol_index

    @property
    def protocol(self) -> dict[str, Any]:
        return SOURCE_REFERENCE_PROTOCOL

    def resolve(self, ref: ArchitectureSourceRef) -> ArchitectureSourceRef:
        """解析并返回带真实文件/行号的已验证引用。"""
        if not isinstance(ref, ArchitectureSourceRef):
            raise ArchitectureReferenceError("源码引用必须是 ArchitectureSourceRef")

        symbol = self._find_symbol(ref)
        if symbol is None:
            label = (
                ref.symbol_id
                or ref.qualified_name
                or f"{ref.file_path}:{ref.start_line}"
            )
            raise ArchitectureReferenceError(
                f"源码引用无法通过 SymbolIndex 校验：{label}"
            )

        if ref.file_path is not None and ref.file_path != symbol.file_path:
            raise ArchitectureReferenceError(
                f"源码引用文件不匹配：期望 {symbol.file_path}，实际 {ref.file_path}"
            )
        if ref.start_line is not None and not (
            symbol.start_line <= ref.start_line <= symbol.end_line
        ):
            raise ArchitectureReferenceError(
                f"源码引用起始行超出符号范围：{ref.start_line} not in "
                f"{symbol.start_line}-{symbol.end_line}"
            )
        if ref.end_line is not None and not (
            symbol.start_line <= ref.end_line <= symbol.end_line
        ):
            raise ArchitectureReferenceError(
                f"源码引用结束行超出符号范围：{ref.end_line} not in "
                f"{symbol.start_line}-{symbol.end_line}"
            )
        return ArchitectureSourceRef.from_symbol(symbol)

    def resolve_many(
        self,
        refs: Iterable[ArchitectureSourceRef],
    ) -> list[ArchitectureSourceRef]:
        return [self.resolve(ref) for ref in refs]

    def _find_symbol(self, ref: ArchitectureSourceRef) -> SymbolRecord | None:
        if ref.symbol_id:
            # symbolId 一旦提供就必须真实存在；不能用 qualifiedName 掩盖
            # 一个虚构的符号 ID。
            return self.symbol_index.get(ref.symbol_id)

        if ref.qualified_name:
            matches = self.symbol_index.find_by_qualified_name(ref.qualified_name)
            symbol = _pick_match(matches, ref.file_path)
            if symbol is not None:
                return symbol

        if ref.file_path and ref.start_line is not None:
            return self.symbol_index.find_at(ref.file_path, ref.start_line)

        return None


@dataclass(frozen=True, slots=True)
class ArchitectureValidationIssue:
    """一条架构校验问题。"""

    severity: str
    code: str
    message: str
    node_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "nodeId": self.node_id,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureValidationReport:
    """校验结果；errors 为空时 graph 是已 canonicalize 的版本。"""

    graph: ArchitectureGraph
    issues: list[ArchitectureValidationIssue]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> list[ArchitectureValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]


class ArchitectureGraphValidator:
    """校验架构图结构、源码引用和 static/inferred 标记。"""

    def __init__(
        self,
        symbol_index: SymbolIndex | SourceRefProtocol | CodeGraph | ProjectIndex,
    ):
        if isinstance(symbol_index, SourceRefProtocol):
            self.protocol = symbol_index
        elif isinstance(symbol_index, CodeGraph):
            self.protocol = SourceRefProtocol(
                SymbolIndex.from_code_graph(symbol_index)
            )
        elif isinstance(symbol_index, ProjectIndex):
            self.protocol = SourceRefProtocol(
                SymbolIndex.from_project_index(symbol_index)
            )
        else:
            self.protocol = SourceRefProtocol(symbol_index)

    def validate(self, graph: ArchitectureGraph) -> ArchitectureGraph:
        report = self.check(graph)
        if not report.ok:
            messages = "; ".join(issue.message for issue in report.errors)
            raise ArchitectureReferenceError(messages)
        return report.graph

    def check(self, graph: ArchitectureGraph) -> ArchitectureValidationReport:
        issues: list[ArchitectureValidationIssue] = []
        canonical_views: list[ArchitectureView] = []

        for view in graph.views:
            canonical_nodes: list[ArchitectureNode] = []
            for node in view.nodes:
                canonical_refs: list[ArchitectureSourceRef] = []
                for ref in node.source_refs:
                    try:
                        canonical_refs.append(self.protocol.resolve(ref))
                    except ArchitectureReferenceError as exc:
                        issues.append(
                            ArchitectureValidationIssue(
                                severity="error",
                                code="invalid_source_ref",
                                message=f"{view.id}/{node.id}: {exc}",
                                node_id=node.id,
                            )
                        )
                        canonical_refs.append(ref)
                if node.detail_view and node.detail_view not in graph.views_by_id:
                    issues.append(
                        ArchitectureValidationIssue(
                            severity="warning",
                            code="missing_detail_view",
                            message=(
                                f"{view.id}/{node.id}: detailView "
                                f"{node.detail_view!r} 不存在"
                            ),
                            node_id=node.id,
                        )
                    )
                for nested in node.nested:
                    if nested.detail_view and nested.detail_view not in graph.views_by_id:
                        issues.append(
                            ArchitectureValidationIssue(
                                severity="warning",
                                code="missing_nested_view",
                                message=(
                                    f"{view.id}/{node.id}: nested detailView "
                                    f"{nested.detail_view!r} 不存在"
                                ),
                                node_id=node.id,
                            )
                        )
                canonical_nodes.append(
                    replace(node, source_refs=canonical_refs)
                )

            canonical_edges: list[ArchitectureEdge] = []
            for edge in view.edges:
                canonical_edge_refs: list[ArchitectureSourceRef] = []
                for ref in edge.source_refs:
                    try:
                        canonical_edge_refs.append(self.protocol.resolve(ref))
                    except ArchitectureReferenceError as exc:
                        issues.append(
                            ArchitectureValidationIssue(
                                severity="error",
                                code="invalid_source_ref",
                                message=f"{view.id}/{edge.id}: {exc}",
                            )
                        )
                        canonical_edge_refs.append(ref)
                edge = replace(edge, source_refs=canonical_edge_refs)
                if (
                    edge.confidence == "static"
                    and not edge.evidence
                    and not edge.source_refs
                ):
                    issues.append(
                        ArchitectureValidationIssue(
                            severity="warning",
                            code="static_without_evidence",
                            message=(
                                f"{view.id}/{edge.id}: static 边缺少 evidence，"
                                "已标记为 inferred"
                            ),
                        )
                    )
                    canonical_edges.append(replace(edge, confidence="inferred"))
                else:
                    canonical_edges.append(edge)

            canonical_views.append(
                ArchitectureView(
                    id=view.id,
                    title=view.title,
                    subtitle=view.subtitle,
                    nodes=canonical_nodes,
                    edges=canonical_edges,
                )
            )

        canonical_graph = replace(graph, views=canonical_views)
        return ArchitectureValidationReport(canonical_graph, issues)


def validate_architecture_graph(
    graph: ArchitectureGraph,
    symbol_index: SymbolIndex | SourceRefProtocol | CodeGraph | ProjectIndex,
) -> ArchitectureGraph:
    """便捷入口：校验并返回 canonical ArchitectureGraph。"""
    return ArchitectureGraphValidator(symbol_index).validate(graph)


def parse_architecture_graph(
    data: dict[str, Any] | str,
    symbol_index: SymbolIndex | SourceRefProtocol | CodeGraph | ProjectIndex,
) -> ArchitectureGraph:
    """解析 LLM/JSON 输出并完成 SymbolIndex 源码校验。"""
    graph = (
        ArchitectureGraph.from_json(data)
        if isinstance(data, str)
        else ArchitectureGraph.from_dict(data)
    )
    return validate_architecture_graph(graph, symbol_index)


SourceReferenceProtocol = SourceRefProtocol


def _pick_match(
    matches: list[SymbolRecord],
    file_path: str | None,
) -> SymbolRecord | None:
    if not matches:
        return None
    if file_path is None:
        return matches[0]
    for symbol in matches:
        if symbol.file_path == file_path:
            return symbol
    return matches[0]


__all__ = [
    "ArchitectureGraphValidator",
    "ArchitectureValidationIssue",
    "ArchitectureValidationReport",
    "SOURCE_REFERENCE_PROTOCOL",
    "SourceReferenceProtocol",
    "SourceRefProtocol",
    "SymbolIndex",
    "parse_architecture_graph",
    "validate_architecture_graph",
]
