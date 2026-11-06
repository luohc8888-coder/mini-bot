"""Phase 3：从 AST 索引构建可查询的 CodeGraph。

CodeGraph 只使用 Phase 2 已经确定的结构事实：

- 节点来自 ``ProjectIndex.symbols``；
- ``contains`` 来自 ``parentId``；
- ``imports`` 通过相对导入和模块表解析；
- ``calls`` 通过局部符号、类作用域、import 别名解析；
- 无法静态确定的引用进入 ``unresolved``，不会生成假边。

动态调用、运行时 monkey patch、任意 ``getattr`` 等不在本阶段解析。
"""

from __future__ import annotations

import builtins
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Iterable

from .models import (
    ImportRecord,
    ProjectIndex,
    SymbolRecord,
)

GRAPH_SCHEMA_VERSION = 1

_BUILTIN_NAMES = frozenset(
    name for name in dir(builtins) if not name.startswith("_")
)


class CodeGraphError(ValueError):
    """CodeGraph 构建或查询失败。"""


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """两个符号之间的有向关系。"""

    id: str
    source: str
    target: str
    kind: str
    confidence: str = "static"
    line: int | None = None
    detail: str | None = None
    occurrences: int = 1

    def to_dict(self) -> dict[str, object]:
        """转换为 JSON 友好的 camelCase 结构。"""
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
    def from_dict(cls, data: dict[str, object]) -> GraphEdge:
        """从 graph.json 数据恢复边。"""
        return cls(
            id=str(data["id"]),
            source=str(data["source"]),
            target=str(data["target"]),
            kind=str(data["kind"]),
            confidence=str(data.get("confidence", "static")),
            line=int(data["line"]) if data.get("line") is not None else None,
            detail=str(data["detail"]) if data.get("detail") is not None else None,
            occurrences=int(data.get("occurrences", 1)),
        )


@dataclass(frozen=True, slots=True)
class UnresolvedReference:
    """无法静态确定来源的 import 或调用。"""

    kind: str
    source_id: str
    expression: str
    reason: str
    line: int | None = None

    def to_dict(self) -> dict[str, object]:
        """转换为 JSON 友好的 camelCase 结构。"""
        return {
            "kind": self.kind,
            "sourceId": self.source_id,
            "expression": self.expression,
            "reason": self.reason,
            "line": self.line,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> UnresolvedReference:
        """从 graph.json 数据恢复未解析引用。"""
        return cls(
            kind=str(data["kind"]),
            source_id=str(data.get("sourceId", "")),
            expression=str(data.get("expression", "")),
            reason=str(data.get("reason", "unknown")),
            line=int(data["line"]) if data.get("line") is not None else None,
        )


class CodeGraph:
    """可查询的符号关系图。

    节点保持 Phase 2 ``ProjectIndex.symbols`` 的顺序；边按构建顺序保存。
    该类是只读查询层，不负责扫描、LLM 解释或 HTTP 服务。
    """

    def __init__(
        self,
        *,
        workspace_root: str,
        workspace_id: str,
        nodes: Iterable[SymbolRecord],
        edges: Iterable[GraphEdge],
        unresolved: Iterable[UnresolvedReference] = (),
    ) -> None:
        self.workspace_root = workspace_root
        self.workspace_id = workspace_id
        self.nodes: dict[str, SymbolRecord] = {}
        for node in nodes:
            if node.id in self.nodes:
                raise CodeGraphError(f"重复的节点 ID：{node.id}")
            self.nodes[node.id] = node
        self.edges = list(edges)
        self.unresolved = list(unresolved)
        self._children: dict[str, list[str]] = defaultdict(list)
        self._outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        self._incoming: dict[str, list[GraphEdge]] = defaultdict(list)

        for node in self.nodes.values():
            if node.parent_id is not None and node.parent_id in self.nodes:
                self._children[node.parent_id].append(node.id)
        for edge in self.edges:
            self._outgoing[edge.source].append(edge)
            self._incoming[edge.target].append(edge)

    @classmethod
    def from_project_index(cls, index: ProjectIndex) -> CodeGraph:
        """从 Phase 2 的 ProjectIndex 构建 CodeGraph。"""
        return CodeGraphBuilder(index).build()

    @property
    def node_list(self) -> list[SymbolRecord]:
        """按索引顺序返回节点列表。"""
        return list(self.nodes.values())

    def get_node(self, node_id: str) -> SymbolRecord | None:
        """按 ID 返回节点。"""
        return self.nodes.get(node_id)

    def get_parent(self, node_id: str) -> SymbolRecord | None:
        """返回父节点；模块节点返回 None。"""
        node = self.nodes.get(node_id)
        if node is None or node.parent_id is None:
            return None
        return self.nodes.get(node.parent_id)

    def get_children(self, node_id: str) -> list[SymbolRecord]:
        """返回直接子节点。"""
        return [
            self.nodes[child_id]
            for child_id in self._children.get(node_id, [])
            if child_id in self.nodes
        ]

    def get_outgoing(
        self,
        node_id: str,
        kind: str | None = None,
    ) -> list[GraphEdge]:
        """返回节点发出的边，可按 kind 过滤。"""
        edges = self._outgoing.get(node_id, [])
        if kind is None:
            return list(edges)
        return [edge for edge in edges if edge.kind == kind]

    def get_incoming(
        self,
        node_id: str,
        kind: str | None = None,
    ) -> list[GraphEdge]:
        """返回指向节点的边，可按 kind 过滤。"""
        edges = self._incoming.get(node_id, [])
        if kind is None:
            return list(edges)
        return [edge for edge in edges if edge.kind == kind]

    def get_callees(self, node_id: str) -> list[SymbolRecord]:
        """返回该节点直接调用的符号。"""
        return [
            self.nodes[edge.target]
            for edge in self.get_outgoing(node_id, kind="calls")
            if edge.target in self.nodes
        ]

    def get_callers(self, node_id: str) -> list[SymbolRecord]:
        """返回直接调用该节点的符号。"""
        return [
            self.nodes[edge.source]
            for edge in self.get_incoming(node_id, kind="calls")
            if edge.source in self.nodes
        ]

    def get_neighbors(self, node_id: str) -> list[SymbolRecord]:
        """返回通过任意边直接相连的节点（去重、保持顺序）。"""
        seen: set[str] = set()
        result: list[SymbolRecord] = []
        for edge in [
            *self.get_outgoing(node_id),
            *self.get_incoming(node_id),
        ]:
            other_id = edge.target if edge.source == node_id else edge.source
            if other_id == node_id or other_id in seen:
                continue
            seen.add(other_id)
            node = self.nodes.get(other_id)
            if node is not None:
                result.append(node)
        return result

    @property
    def stats(self) -> dict[str, int]:
        """返回节点、边和未解析引用统计。"""
        counts = {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "contains": 0,
            "imports": 0,
            "calls": 0,
            "unresolved": len(self.unresolved),
            "unresolvedImports": 0,
            "unresolvedCalls": 0,
            "modules": 0,
            "classes": 0,
            "functions": 0,
            "methods": 0,
        }
        for node in self.nodes.values():
            key = {
                "class": "classes",
                "function": "functions",
                "method": "methods",
                "module": "modules",
            }.get(node.kind)
            if key is not None:
                counts[key] += 1
        for edge in self.edges:
            if edge.kind in counts:
                counts[edge.kind] += 1
        for item in self.unresolved:
            if item.kind == "import":
                counts["unresolvedImports"] += 1
            elif item.kind == "call":
                counts["unresolvedCalls"] += 1
        return counts

    def to_dict(self) -> dict[str, object]:
        """转换为 JSON 友好的 graph 结构。"""
        return {
            "schemaVersion": GRAPH_SCHEMA_VERSION,
            "workspaceRoot": self.workspace_root,
            "workspaceId": self.workspace_id,
            "stats": self.stats,
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CodeGraph:
        """从 graph.json 数据恢复 CodeGraph。"""
        return cls(
            workspace_root=str(data.get("workspaceRoot", "")),
            workspace_id=str(data.get("workspaceId", "")),
            nodes=[
                SymbolRecord.from_dict(item)
                for item in data.get("nodes", [])
            ],
            edges=[
                GraphEdge.from_dict(item)
                for item in data.get("edges", [])
            ],
            unresolved=[
                UnresolvedReference.from_dict(item)
                for item in data.get("unresolved", [])
            ],
        )


class CodeGraphBuilder:
    """从 ``ProjectIndex`` 构建 ``CodeGraph``。"""

    def __init__(self, index: ProjectIndex) -> None:
        self.index = index
        self.symbols = list(index.symbols)
        self.by_id: dict[str, SymbolRecord] = {}
        self.by_qualified: dict[str, SymbolRecord] = {}
        self.children_by_parent: dict[str, list[SymbolRecord]] = defaultdict(list)
        self.modules_by_name: dict[str, SymbolRecord] = {}
        self.module_by_file: dict[str, SymbolRecord] = {}
        self.top_level_names: set[str] = set()
        self.bindings_by_file: dict[str, dict[str, str]] = defaultdict(dict)
        self.bound_symbols_by_file: dict[str, dict[str, SymbolRecord]] = (
            defaultdict(dict)
        )
        self.module_exports: dict[str, dict[str, SymbolRecord]] = defaultdict(dict)
        self.external_names_by_file: dict[str, set[str]] = defaultdict(set)
        self._unresolved: list[UnresolvedReference] = []

        for symbol in self.symbols:
            if symbol.id in self.by_id:
                raise CodeGraphError(f"重复的节点 ID：{symbol.id}")
            self.by_id[symbol.id] = symbol
            self.by_qualified.setdefault(symbol.qualified_name, symbol)
            if symbol.parent_id is not None:
                self.children_by_parent[symbol.parent_id].append(symbol)
            if symbol.kind == "module":
                self.modules_by_name.setdefault(symbol.name, symbol)
                self.module_by_file.setdefault(symbol.file_path, symbol)
                if symbol.name:
                    self.top_level_names.add(symbol.name.split(".", 1)[0])

    def build(self) -> CodeGraph:
        """构建并返回 CodeGraph。"""
        self.bindings_by_file.clear()
        self.bound_symbols_by_file.clear()
        self.module_exports.clear()
        self.external_names_by_file.clear()
        self._unresolved = []
        self._build_module_exports()
        self._build_import_bindings()
        edge_map: dict[tuple[str, str, str], GraphEdge] = {}
        self._build_contains_edges(edge_map)
        self._build_import_edges(edge_map)
        self._build_call_edges(edge_map)
        return CodeGraph(
            workspace_root=self.index.workspace_root,
            workspace_id=self.index.workspace_id,
            nodes=self.symbols,
            edges=list(edge_map.values()),
            unresolved=self._unresolved,
        )

    def _build_contains_edges(
        self,
        edge_map: dict[tuple[str, str, str], GraphEdge],
    ) -> None:
        """根据 parentId 生成 contains 边。"""
        for symbol in self.symbols:
            if symbol.parent_id is None or symbol.parent_id not in self.by_id:
                continue
            edge = GraphEdge(
                id=_edge_id("contains", symbol.parent_id, symbol.id),
                source=symbol.parent_id,
                target=symbol.id,
                kind="contains",
                confidence="static",
                line=symbol.start_line,
                detail=None,
            )
            _add_edge(edge_map, edge)

    def _build_module_exports(self) -> None:
        """构建模块命名空间中的导出名 → 实际符号映射。

        例如 ``pkg/__init__.py`` 中执行 ``from .models import Item`` 后，
        ``from pkg import Item`` 应能解析到 ``pkg.models.Item``。
        """
        # 模块自身定义的顶层类/函数也是该模块的公开名称。
        for module in self.modules_by_name.values():
            for child in self.children_by_parent.get(module.id, []):
                self.module_exports[module.name][child.name] = child

        # 允许 ``pkg/__init__.py`` → ``pkg.service`` → ``pkg.models``
        # 这类多级 re-export；导入顺序不保证，因此做有限次收敛。
        for _ in range(3):
            changed = False
            for record in self.index.imports:
                if record.name is None:
                    continue
                source = self.module_by_file.get(record.file_path)
                target_name = self._resolve_import_module_name(record)
                if source is None or target_name is None:
                    continue
                if target_name not in self.modules_by_name:
                    continue
                if record.name == "*":
                    for export_name, bound in self.module_exports.get(
                        target_name, {}
                    ).items():
                        if self.module_exports[source.name].get(
                            export_name
                        ) is not bound:
                            self.module_exports[source.name][
                                export_name
                            ] = bound
                            changed = True
                    continue

                bound = self.by_qualified.get(
                    f"{target_name}.{record.name}"
                )
                if bound is None:
                    bound = self.module_exports.get(
                        target_name, {}
                    ).get(record.name)
                if bound is None:
                    bound = self.modules_by_name.get(
                        f"{target_name}.{record.name}"
                    )
                if bound is None:
                    continue
                export_name = record.alias or record.name
                if self.module_exports[source.name].get(export_name) is not bound:
                    self.module_exports[source.name][export_name] = bound
                    changed = True
            if not changed:
                break

    def _build_import_bindings(self) -> None:
        """构建每个文件内的 import 别名 → 项目内符号/模块映射。"""
        for record in self.index.imports:
            target_name = self._resolve_import_module_name(record)
            target_module = (
                self.modules_by_name.get(target_name)
                if target_name is not None
                else None
            )
            if target_module is None:
                # 命名空间包可能没有 ``__init__.py`` 模块节点；
                # ``from pkg import models`` 仍可落到 ``pkg.models``。
                child = self._child_module(record, target_name)
                if child is not None:
                    self.bound_symbols_by_file[record.file_path][
                        record.alias or record.name
                    ] = child
                    continue
                self._record_external_binding(record)
                continue

            if record.name is None:
                # import pkg.service / import pkg.service as svc
                key = record.alias or record.module.split(".", 1)[0]
                if key:
                    self.bindings_by_file[record.file_path][key] = (
                        target_name
                        if record.alias
                        else key
                    )
                continue

            if record.name == "*":
                # from pkg import *：把目标模块已知导出名绑定到当前文件。
                for export_name, bound in self.module_exports.get(
                    target_module.name, {}
                ).items():
                    self.bound_symbols_by_file[record.file_path][
                        export_name
                    ] = bound
                continue
            bound = self.module_exports.get(target_name, {}).get(record.name)
            if bound is not None:
                self.bound_symbols_by_file[record.file_path][
                    record.alias or record.name
                ] = bound
                continue
            candidate = f"{target_name}.{record.name}"
            if candidate in self.modules_by_name:
                self.bindings_by_file[record.file_path][
                    record.alias or record.name
                ] = candidate
            elif candidate in self.by_qualified:
                self.bindings_by_file[record.file_path][
                    record.alias or record.name
                ] = candidate

    def _record_external_binding(self, record: ImportRecord) -> None:
        """记录外部 import 的根名称，避免调用解析将它们误报为缺失。"""
        if record.name is not None:
            key = record.alias or record.name
        elif record.alias:
            key = record.alias
        else:
            key = record.module.split(".", 1)[0]
        if key:
            self.external_names_by_file[record.file_path].add(key)

    def _build_import_edges(
        self,
        edge_map: dict[tuple[str, str, str], GraphEdge],
    ) -> None:
        """生成项目内部模块之间的 imports 边。"""
        for record in self.index.imports:
            source = self.module_by_file.get(record.file_path)
            target_name = self._resolve_import_module_name(record)
            if source is None or target_name is None:
                if source is not None and record.level > 0:
                    self._add_unresolved_import(record, source)
                continue

            target = self.modules_by_name.get(target_name)
            if target is None:
                child = self._child_module(record, target_name)
                if child is not None:
                    target = child
                    target_name = child.name
                elif self._looks_internal(target_name) or record.level > 0:
                    self._add_unresolved_import(record, source)
                    continue
                else:
                    continue

            if record.name and record.name != "*":
                # ``from pkg import submodule`` 实际依赖的是子模块，
                # 而不是 ``pkg`` 本身；能识别时把边指向子模块。
                child_module = self.modules_by_name.get(
                    f"{target_name}.{record.name}"
                )
                if child_module is not None:
                    target = child_module
                    target_name = child_module.name

            edge = GraphEdge(
                id=_edge_id("imports", source.id, target.id),
                source=source.id,
                target=target.id,
                kind="imports",
                confidence="static",
                line=record.line,
                detail=_import_expression(record),
            )
            _add_edge(edge_map, edge)

    def _build_call_edges(
        self,
        edge_map: dict[tuple[str, str, str], GraphEdge],
    ) -> None:
        """生成静态调用边，无法解析的调用进入 unresolved。"""
        for call in self.index.calls:
            caller = self.by_id.get(call.caller_id)
            if caller is None:
                continue
            target, status = self._resolve_call(caller, call.callee_name)
            if target is None:
                if status == "unresolved":
                    self._unresolved.append(
                        UnresolvedReference(
                            kind="call",
                            source_id=caller.id,
                            expression=call.callee_name
                            or call.callee_expression,
                            reason="unresolved_call",
                            line=call.line,
                        )
                    )
                continue
            edge = GraphEdge(
                id=_edge_id("calls", caller.id, target.id),
                source=caller.id,
                target=target.id,
                kind="calls",
                confidence="static",
                line=call.line,
                detail=call.callee_expression or call.callee_name,
            )
            _add_edge(edge_map, edge)

    def _resolve_call(
        self,
        caller: SymbolRecord,
        callee_name: str,
    ) -> tuple[SymbolRecord | None, str]:
        """解析一个调用名称。

        Returns:
            ``(target, status)``，status 为 ``static``、``builtin``、
            ``external`` 或 ``unresolved``。
        """
        name = callee_name.strip()
        if not name:
            return None, "unresolved"
        parts = name.split(".")
        first = parts[0]

        if first in _BUILTIN_NAMES:
            return None, "builtin"

        if name in self.by_qualified:
            return self.by_qualified[name], "static"

        bound = self.bound_symbols_by_file.get(caller.file_path, {}).get(first)
        if bound is not None:
            if len(parts) == 1:
                return bound, "static"
            candidate = f"{bound.qualified_name}.{'.'.join(parts[1:])}"
            target = self.by_qualified.get(candidate)
            if target is not None:
                return target, "static"
            return None, "unresolved"

        if first in ("self", "cls"):
            class_symbol = self._enclosing_class(caller)
            if class_symbol is not None and len(parts) > 1:
                candidate = f"{class_symbol.qualified_name}.{'.'.join(parts[1:])}"
                target = self.by_qualified.get(candidate)
                if target is not None:
                    return target, "static"
            return None, "unresolved"

        bindings = self.bindings_by_file.get(caller.file_path, {})
        if first in bindings:
            prefix = bindings[first]
            candidate = prefix if len(parts) == 1 else (
                f"{prefix}.{'.'.join(parts[1:])}"
            )
            target = self.by_qualified.get(candidate)
            if target is not None:
                return target, "static"
            return None, "unresolved"

        module = self.module_by_file.get(caller.file_path)
        if module is not None:
            for candidate in (
                f"{module.qualified_name}.{name}",
                f"{module.qualified_name}.{first}",
            ):
                target = self.by_qualified.get(candidate)
                if target is not None:
                    return target, "static"

        # 嵌套函数：inner() 的父节点通常是当前函数。
        for scope in self._scope_chain(caller):
            for child in self.children_by_parent.get(scope.id, []):
                if child.name != first:
                    continue
                if len(parts) == 1:
                    return child, "static"
                candidate = f"{child.qualified_name}.{'.'.join(parts[1:])}"
                target = self.by_qualified.get(candidate)
                if target is not None:
                    return target, "static"

        if first in self.external_names_by_file.get(caller.file_path, set()):
            return None, "external"
        return None, "unresolved"

    def _enclosing_class(self, symbol: SymbolRecord) -> SymbolRecord | None:
        """向父链查找最近的 class 节点。"""
        current = symbol
        while True:
            if current.kind == "class":
                return current
            if current.parent_id is None:
                return None
            parent = self.by_id.get(current.parent_id)
            if parent is None:
                return None
            current = parent

    def _scope_chain(self, symbol: SymbolRecord) -> list[SymbolRecord]:
        """返回从当前符号到 module 的父链。"""
        result: list[SymbolRecord] = []
        current: SymbolRecord | None = symbol
        while current is not None:
            result.append(current)
            current = (
                self.by_id.get(current.parent_id)
                if current.parent_id is not None
                else None
            )
        return result

    def _child_module(
        self,
        record: ImportRecord,
        target_name: str | None,
    ) -> SymbolRecord | None:
        """返回 ``from pkg import submodule`` 对应的子模块。"""
        if not target_name or record.name is None or record.name == "*":
            return None
        return self.modules_by_name.get(f"{target_name}.{record.name}")

    def _resolve_import_module_name(
        self,
        record: ImportRecord,
    ) -> str | None:
        """把 import record 解析成项目内模块名。"""
        if record.level == 0:
            return record.module or None

        source = self.module_by_file.get(record.file_path)
        if source is None:
            return None
        package = _package_for_module(source)
        return _relative_module_name(package, record.level, record.module)

    def _looks_internal(self, module_name: str) -> bool:
        """判断缺失模块是否看起来属于当前项目。"""
        return module_name.split(".", 1)[0] in self.top_level_names

    def _add_unresolved_import(
        self,
        record: ImportRecord,
        source: SymbolRecord,
    ) -> None:
        """记录无法解析的内部 import。"""
        self._unresolved.append(
            UnresolvedReference(
                kind="import",
                source_id=source.id,
                expression=_import_expression(record),
                reason=(
                    "relative_module_not_found"
                    if record.level > 0
                    else "module_not_found"
                ),
                line=record.line,
            )
        )


def _package_for_module(module: SymbolRecord) -> str:
    """返回模块所属包名；包初始化文件返回自身模块名。"""
    if module.file_path.endswith("__init__.py"):
        return module.name
    if "." not in module.name:
        return ""
    return module.name.rsplit(".", 1)[0]


def _relative_module_name(
    package: str,
    level: int,
    module: str,
) -> str | None:
    """计算相对 import 的绝对模块名。"""
    if level < 1:
        return module or None
    parts = package.split(".") if package else []
    drop = level - 1
    if drop > len(parts):
        return None
    base = parts[: len(parts) - drop] if drop else parts
    if module:
        base.extend(module.split("."))
    return ".".join(base) or None


def _import_expression(record: ImportRecord) -> str:
    """生成 import 的可读表达式，用于日志和边详情。"""
    if record.name is None:
        return f"import {record.module}"
    prefix = "." * record.level
    module = f"{prefix}{record.module}" if record.module else prefix
    if not module:
        module = "."
    return f"from {module} import {record.name}"


def _edge_id(kind: str, source: str, target: str) -> str:
    """生成稳定边 ID。"""
    return f"{kind}:{source}->{target}"


def _add_edge(
    edge_map: dict[tuple[str, str, str], GraphEdge],
    edge: GraphEdge,
) -> None:
    """按 (kind, source, target) 合并重复关系。"""
    key = (edge.kind, edge.source, edge.target)
    existing = edge_map.get(key)
    if existing is None:
        edge_map[key] = edge
        return
    edge_map[key] = replace(
        existing,
        line=existing.line if existing.line is not None else edge.line,
        detail=existing.detail or edge.detail,
        occurrences=existing.occurrences + edge.occurrences,
    )


def build_code_graph(index: ProjectIndex) -> CodeGraph:
    """从 ProjectIndex 构建 CodeGraph 的便捷函数。"""
    return CodeGraph.from_project_index(index)


__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "CodeGraph",
    "CodeGraphBuilder",
    "CodeGraphError",
    "GraphEdge",
    "UnresolvedReference",
    "build_code_graph",
]
