"""Phase 4.2：ArchitectureGraph 数据模型与源码引用协议。

ArchitectureGraph 是 AI 语义架构分析的结构化输出。本模块只定义
可序列化、可校验的协议对象，不调用 LLM，也不生成任何架构结论。

关键约束：

- 逻辑组件、阶段和条件都只是架构层节点，不是 CodeGraph 节点本体；
- 每个 ``ArchitectureSourceRef`` 必须能通过 ``SymbolIndex`` 解析到真实符号；
- 无法静态确认的架构关系使用 ``confidence="inferred"``；
- 视图（view）可以表达项目总览和局部执行流程，组件通过 ``nested`` 表示
  视觉嵌套，源码事实仍由 ``source_refs`` 指向的符号提供。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

ARCHITECTURE_SCHEMA_VERSION = 1
ARCHITECTURE_GRAPH_SCHEMA_VERSION = ARCHITECTURE_SCHEMA_VERSION
SOURCE_REFERENCE_PROTOCOL_VERSION = 1
SOURCE_REF_PROTOCOL_VERSION = SOURCE_REFERENCE_PROTOCOL_VERSION

ARCHITECTURE_NODE_KINDS = frozenset({"component", "stage", "condition"})
ARCHITECTURE_EDGE_KINDS = frozenset({"flow", "data", "branch", "dependency"})
ARCHITECTURE_CONFIDENCES = frozenset({"static", "inferred"})


class ArchitectureError(ValueError):
    """ArchitectureGraph 构建、解析或校验失败。"""


class ArchitectureReferenceError(ArchitectureError):
    """源码引用无法通过 SymbolIndex 校验。"""


class ArchitectureValidationError(ArchitectureError):
    """ArchitectureGraph 结构非法。"""


def _validate_enum(value: str, allowed: frozenset[str], label: str) -> None:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ArchitectureValidationError(f"{label} 必须是 {choices} 之一，实际为 {value!r}")


@dataclass(frozen=True, slots=True)
class ArchitectureSourceRef:
    """一条架构节点到真实代码符号的引用。

    ``symbol_id`` 是首选定位方式；``qualified_name`` 与 ``file_path``/
    ``start_line``/``end_line`` 作为辅助定位与审计字段。解析成功后，
    ``verified`` 为 True，且文件与行号会被标准化为 SymbolRecord 的真实值。
    """

    symbol_id: str
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    qualified_name: str | None = None
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为前端/LLM 共用的 camelCase JSON 结构。"""
        return {
            "symbolId": self.symbol_id,
            "filePath": self.file_path,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "qualifiedName": self.qualified_name,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectureSourceRef:
        """从 JSON 数据恢复源码引用。"""
        return cls(
            symbol_id=str(data.get("symbolId", data.get("symbol_id", ""))),
            file_path=data.get("filePath", data.get("file_path")),
            start_line=_optional_int(data.get("startLine", data.get("start_line"))),
            end_line=_optional_int(data.get("endLine", data.get("end_line"))),
            qualified_name=data.get(
                "qualifiedName",
                data.get("qualified_name"),
            ),
            verified=bool(data.get("verified", False)),
        )

    @classmethod
    def from_symbol(cls, symbol: Any) -> ArchitectureSourceRef:
        """从 SymbolRecord 构造一个已验证的引用。"""
        return cls(
            symbol_id=symbol.id,
            file_path=symbol.file_path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            qualified_name=symbol.qualified_name,
            verified=True,
        )


@dataclass(frozen=True, slots=True)
class ArchitectureNestedNode:
    """组件卡片内部展示的嵌套组件入口。"""

    id: str
    label: str
    kind: str = "component"
    detail_view: str | None = None

    def __post_init__(self) -> None:
        _validate_enum(self.kind, ARCHITECTURE_NODE_KINDS, "nested.kind")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "detailView": self.detail_view,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectureNestedNode:
        return cls(
            id=str(data["id"]),
            label=str(data.get("label", data["id"])),
            kind=str(data.get("kind", "component")),
            detail_view=data.get("detailView", data.get("detail_view")),
        )


@dataclass(frozen=True, slots=True)
class ArchitectureNode:
    """一个逻辑组件、执行阶段或判断条件。"""

    id: str
    kind: str
    label: str
    description: str = ""
    meta: str | None = None
    parent_id: str | None = None
    detail_view: str | None = None
    responsibilities: list[str] = field(default_factory=list)
    source_refs: list[ArchitectureSourceRef] = field(default_factory=list)
    nested: list[ArchitectureNestedNode] = field(default_factory=list)
    confidence: str = "inferred"
    evidence: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_enum(self.kind, ARCHITECTURE_NODE_KINDS, "node.kind")
        _validate_enum(self.confidence, ARCHITECTURE_CONFIDENCES, "node.confidence")
        if not self.id:
            raise ArchitectureValidationError("node.id 不能为空")
        if not self.label:
            raise ArchitectureValidationError("node.label 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "meta": self.meta,
            "parentId": self.parent_id,
            "detailView": self.detail_view,
            "responsibilities": list(self.responsibilities),
            "sourceRefs": [ref.to_dict() for ref in self.source_refs],
            "nested": [nested.to_dict() for nested in self.nested],
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectureNode:
        return cls(
            id=str(data["id"]),
            kind=str(data.get("kind", "stage")),
            label=str(data.get("label", data["id"])),
            description=str(data.get("description", "")),
            meta=data.get("meta"),
            parent_id=data.get("parentId", data.get("parent_id")),
            detail_view=data.get("detailView", data.get("detail_view")),
            responsibilities=list(data.get("responsibilities", [])),
            source_refs=[
                ArchitectureSourceRef.from_dict(item)
                for item in data.get("sourceRefs", data.get("source_refs", []))
            ],
            nested=[
                ArchitectureNestedNode.from_dict(item)
                for item in data.get("nested", [])
            ],
            confidence=str(data.get("confidence", "inferred")),
            evidence=list(data.get("evidence", [])),
        )


@dataclass(frozen=True, slots=True)
class ArchitectureEdge:
    """两个架构节点之间的有向关系。"""

    id: str
    source: str
    target: str
    kind: str
    confidence: str = "inferred"
    label: str | None = None
    evidence: list[str] = field(default_factory=list)
    source_refs: list[ArchitectureSourceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_enum(self.kind, ARCHITECTURE_EDGE_KINDS, "edge.kind")
        _validate_enum(self.confidence, ARCHITECTURE_CONFIDENCES, "edge.confidence")
        if not self.id:
            raise ArchitectureValidationError("edge.id 不能为空")
        if self.source == self.target:
            raise ArchitectureValidationError("edge.source 不能等于 edge.target")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "confidence": self.confidence,
            "label": self.label,
            "evidence": list(self.evidence),
            "sourceRefs": [ref.to_dict() for ref in self.source_refs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectureEdge:
        return cls(
            id=str(data["id"]),
            source=str(data["source"]),
            target=str(data["target"]),
            kind=str(data.get("kind", "flow")),
            confidence=str(data.get("confidence", "inferred")),
            label=data.get("label"),
            evidence=list(data.get("evidence", [])),
            source_refs=[
                ArchitectureSourceRef.from_dict(item)
                for item in data.get("sourceRefs", data.get("source_refs", []))
            ],
        )


@dataclass(frozen=True, slots=True)
class ArchitectureView:
    """一个可独立下钻的架构视图（总览或局部执行流程）。"""

    id: str
    title: str
    subtitle: str = ""
    nodes: list[ArchitectureNode] = field(default_factory=list)
    edges: list[ArchitectureEdge] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            raise ArchitectureValidationError("view.id 不能为空")
        self._validate_graph_shape()

    def _validate_graph_shape(self) -> None:
        node_ids: set[str] = set()
        for node in self.nodes:
            if node.id in node_ids:
                raise ArchitectureValidationError(
                    f"view {self.id!r} 存在重复节点 ID：{node.id}"
                )
            node_ids.add(node.id)
        for edge in self.edges:
            if edge.source not in node_ids or edge.target not in node_ids:
                raise ArchitectureValidationError(
                    f"view {self.id!r} 的边 {edge.id!r} 引用了不存在的节点"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectureView:
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", data["id"])),
            subtitle=str(data.get("subtitle", "")),
            nodes=[
                ArchitectureNode.from_dict(item)
                for item in data.get("nodes", [])
            ],
            edges=[
                ArchitectureEdge.from_dict(item)
                for item in data.get("edges", [])
            ],
        )


@dataclass(frozen=True, slots=True)
class ArchitectureGraph:
    """可序列化的项目架构图。"""

    project_name: str
    project_root: str
    project_description: str
    workspace_root: str
    workspace_id: str
    generated_at: str
    views: list[ArchitectureView] | dict[str, ArchitectureView]
    default_view_id: str = "overview"
    schema_version: int = ARCHITECTURE_SCHEMA_VERSION
    mock: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.views, dict):
            object.__setattr__(self, "views", list(self.views.values()))
        if not self.views:
            raise ArchitectureValidationError("ArchitectureGraph 至少需要一个视图")
        view_ids = {view.id for view in self.views}
        if len(view_ids) != len(self.views):
            raise ArchitectureValidationError("ArchitectureGraph 的视图 ID 不能重复")
        if self.default_view_id not in view_ids:
            # 省略 default_view_id 时使用第一个视图，便于测试和单视图图。
            if self.default_view_id == "overview":
                object.__setattr__(self, "default_view_id", self.views[0].id)
            else:
                raise ArchitectureValidationError(
                    f"default_view_id {self.default_view_id!r} 不存在"
                )
        # 节点 ID 只需在单个视图内唯一；不同视图可以复用同一逻辑组件。

    @property
    def views_by_id(self) -> dict[str, ArchitectureView]:
        return {view.id: view for view in self.views}

    def get_view(self, view_id: str) -> ArchitectureView | None:
        return self.views_by_id.get(view_id)

    def to_dict(self) -> dict[str, Any]:
        """转换为与 Phase 4.1 前端 Mock JSON 兼容的结构。"""
        return {
            "schemaVersion": self.schema_version,
            "version": str(self.schema_version),
            "mock": self.mock,
            "workspaceRoot": self.workspace_root,
            "workspaceId": self.workspace_id,
            "generatedAt": self.generated_at,
            "project": {
                "name": self.project_name,
                "root": self.project_root,
                "description": self.project_description,
            },
            "defaultViewId": self.default_view_id,
            "views": {
                view.id: view.to_dict()
                for view in self.views
            },
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> ArchitectureGraph:
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectureGraph:
        project = data.get("project") or {}
        if not isinstance(project, dict):
            project = {}
        raw_views = data.get("views") or {}
        if isinstance(raw_views, dict):
            views = [
                ArchitectureView.from_dict(item)
                for item in raw_views.values()
            ]
        elif isinstance(raw_views, list):
            views = [
                ArchitectureView.from_dict(item)
                for item in raw_views
            ]
        else:
            views = []
        return cls(
            project_name=str(project.get("name", data.get("name", ""))),
            project_root=str(project.get("root", data.get("workspaceRoot", ""))),
            project_description=str(project.get("description", "")),
            workspace_root=str(
                data.get("workspaceRoot", project.get("root", ""))
            ),
            workspace_id=str(data.get("workspaceId", "")),
            generated_at=str(data.get("generatedAt", "")),
            views=views,
            default_view_id=str(
                data.get("defaultViewId", data.get("default_view_id", "overview"))
            ),
            schema_version=_schema_version(
                data.get("schemaVersion", data.get("version", 1))
            ),
            mock=bool(data.get("mock", False)),
        )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _schema_version(value: Any) -> int:
    """兼容前端 mock 的字符串版本号；未知版本回退到当前 schema。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return ARCHITECTURE_SCHEMA_VERSION


__all__ = [
    "ARCHITECTURE_CONFIDENCES",
    "ARCHITECTURE_EDGE_KINDS",
    "ARCHITECTURE_GRAPH_SCHEMA_VERSION",
    "ARCHITECTURE_NODE_KINDS",
    "ARCHITECTURE_SCHEMA_VERSION",
    "SOURCE_REFERENCE_PROTOCOL_VERSION",
    "SOURCE_REF_PROTOCOL_VERSION",
    "ArchitectureEdge",
    "ArchitectureError",
    "ArchitectureGraph",
    "ArchitectureNestedNode",
    "ArchitectureNode",
    "ArchitectureReferenceError",
    "ArchitectureSourceRef",
    "ArchitectureValidationError",
    "ArchitectureView",
]
