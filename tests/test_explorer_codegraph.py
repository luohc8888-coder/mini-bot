"""CodeGraph 构建、边解析与查询测试。"""

from __future__ import annotations

import json
from pathlib import Path

from mini_harness.explorer import (
    CodeGraph,
    build_code_graph,
    scan_project,
)
from mini_harness.workspace import WorkspaceResolver


def _workspace(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    return WorkspaceResolver(cwd=tmp_path).resolve(root), root


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture_project(tmp_path: Path) -> CodeGraph:
    workspace, root = _workspace(tmp_path)
    _write(
        root,
        "main.py",
        '''"""入口模块。"""
from pkg import Item
from pkg.service import Service, run
import pkg.helpers as helpers
import os


def main() -> None:
    service = Service()
    run(1)
    helpers.upper("x")
    os.getcwd()
    Item("re-exported")
''',
    )
    _write(
        root,
        "pkg/__init__.py",
        '''"""Package."""
from .models import Item
''',
    )
    _write(
        root,
        "pkg/models.py",
        '''class Item:
    """Item model."""

    def __init__(self, name: str):
        self.name = name

    def label(self) -> str:
        return self.name
''',
    )
    _write(
        root,
        "pkg/helpers.py",
        '''def upper(value: str) -> str:
    return value.upper()
''',
    )
    _write(
        root,
        "pkg/service.py",
        '''from .models import Item
from . import helpers


class Service:
    """Service."""

    def __init__(self):
        self.items = []

    def add(self, item: Item) -> None:
        self._validate(item)
        self.items.append(item)

    def _validate(self, item: Item) -> None:
        if not item.label():
            raise ValueError("bad")

    def build(self) -> Item:
        return Item("x")


def make_service() -> Service:
    return Service()


def run(value: int) -> str:
    service = make_service()
    service.add(Item(str(value)))
    return helpers.upper(str(value))
''',
    )
    _write(
        root,
        "pkg/sub/__init__.py",
        '''"""Subpackage."""
''',
    )
    _write(
        root,
        "pkg/sub/deep.py",
        '''from ..models import Item


def use(item: Item) -> Item:
    return Item(item.name)
''',
    )
    _write(
        root,
        "pkg/missing_import.py",
        '''from .missing import Thing
''',
    )
    index = scan_project(workspace)
    return build_code_graph(index)


def _node_id(graph: CodeGraph, qualified_name: str) -> str:
    for node in graph.nodes.values():
        if node.qualified_name == qualified_name:
            return node.id
    raise AssertionError(f"找不到节点：{qualified_name}")


def _edge_targets(
    graph: CodeGraph,
    source_id: str,
    kind: str,
) -> set[str]:
    return {
        edge.target for edge in graph.get_outgoing(source_id, kind=kind)
    }


def test_codegraph_nodes_and_contains_edges_match_symbols(tmp_path: Path) -> None:
    """图中每个符号都应有节点，parentId 应生成 contains 边。"""
    graph = _fixture_project(tmp_path)

    assert len(graph.nodes) == len(graph.node_list)
    assert graph.stats["nodes"] > 0
    assert graph.stats["contains"] > 0
    assert graph.stats["imports"] > 0
    assert graph.stats["calls"] > 0

    service_id = _node_id(graph, "pkg.service.Service")
    add_id = _node_id(graph, "pkg.service.Service.add")
    assert add_id in {
        child.id for child in graph.get_children(service_id)
    }
    assert graph.get_parent(add_id).id == service_id
    assert any(
        edge.source == service_id and edge.target == add_id
        for edge in graph.get_outgoing(service_id, kind="contains")
    )


def test_codegraph_resolves_relative_and_alias_imports(tmp_path: Path) -> None:
    """相对 import、绝对 import 和别名 import 应生成内部模块边。"""
    graph = _fixture_project(tmp_path)

    service_id = _node_id(graph, "pkg.service")
    models_id = _node_id(graph, "pkg.models")
    helpers_id = _node_id(graph, "pkg.helpers")
    main_id = _node_id(graph, "main")

    assert models_id in _edge_targets(graph, service_id, "imports")
    assert helpers_id in _edge_targets(graph, service_id, "imports")
    assert service_id in _edge_targets(graph, main_id, "imports")
    assert helpers_id in _edge_targets(graph, main_id, "imports")

    item_id = _node_id(graph, "pkg.models.Item")
    main_function_id = _node_id(graph, "main.main")
    assert item_id in _edge_targets(graph, main_function_id, "calls")
    deep_id = _node_id(graph, "pkg.sub.deep")
    assert models_id in _edge_targets(graph, deep_id, "imports")
    deep_use_id = _node_id(graph, "pkg.sub.deep.use")
    assert item_id in _edge_targets(graph, deep_use_id, "calls")


def test_codegraph_resolves_static_calls_without_fake_dynamic_edges(
    tmp_path: Path,
) -> None:
    """self 方法、同模块函数、import 别名可解析；动态对象调用不伪造边。"""
    graph = _fixture_project(tmp_path)

    add_id = _node_id(graph, "pkg.service.Service.add")
    validate_id = _node_id(graph, "pkg.service.Service._validate")
    build_id = _node_id(graph, "pkg.service.Service.build")
    item_id = _node_id(graph, "pkg.models.Item")
    run_id = _node_id(graph, "pkg.service.run")
    make_service_id = _node_id(graph, "pkg.service.make_service")
    helpers_upper_id = _node_id(graph, "pkg.helpers.upper")
    main_function_id = _node_id(graph, "main.main")
    service_id = _node_id(graph, "pkg.service.Service")
    service_run_id = _node_id(graph, "pkg.service.run")

    assert validate_id in _edge_targets(graph, add_id, "calls")
    assert item_id in _edge_targets(graph, build_id, "calls")
    assert make_service_id in _edge_targets(graph, run_id, "calls")
    assert helpers_upper_id in _edge_targets(graph, run_id, "calls")
    assert service_id in _edge_targets(graph, main_function_id, "calls")
    assert service_run_id in _edge_targets(graph, main_function_id, "calls")
    assert helpers_upper_id in _edge_targets(graph, main_function_id, "calls")

    # service.add(...) 的接收者没有静态类型，不能被臆测为 Service.add。
    assert add_id not in _edge_targets(graph, run_id, "calls")

    dynamic_calls = [
        item
        for item in graph.unresolved
        if item.kind == "call" and item.expression == "service.add"
    ]
    assert dynamic_calls


def test_codegraph_reports_unresolved_relative_import(tmp_path: Path) -> None:
    """相对 import 指向不存在的内部模块时应留下未解析记录。"""
    graph = _fixture_project(tmp_path)

    unresolved = [
        item
        for item in graph.unresolved
        if item.kind == "import"
        and item.expression == "from .missing import Thing"
    ]
    assert len(unresolved) == 1
    assert unresolved[0].reason == "relative_module_not_found"


def test_codegraph_queries_and_serialization(tmp_path: Path) -> None:
    """调用者和被调用者查询、邻接查询及 JSON 序列化应可用。"""
    graph = _fixture_project(tmp_path)
    add_id = _node_id(graph, "pkg.service.Service.add")

    callers = {node.id for node in graph.get_callers(add_id)}
    assert callers == set()
    callees = graph.get_callees(add_id)
    assert any(
        node.qualified_name == "pkg.service.Service._validate"
        for node in callees
    )
    neighbors = {node.id for node in graph.get_neighbors(add_id)}
    assert add_id not in neighbors

    data = graph.to_dict()
    assert data["schemaVersion"] == 1
    assert data["stats"]["nodes"] == len(graph.nodes)
    json.dumps(data, ensure_ascii=False)
    restored = CodeGraph.from_dict(data)
    assert len(restored.nodes) == len(graph.nodes)
    assert len(restored.edges) == len(graph.edges)


def test_codegraph_resolves_namespace_package_child_import(
    tmp_path: Path,
) -> None:
    """没有 __init__.py 的命名空间包仍能解析子模块 import。"""
    workspace, root = _workspace(tmp_path)
    _write(root, "pkg/models.py", "class Item:\n    pass\n")
    _write(
        root,
        "pkg/service.py",
        "from . import models\n\n"
        "def use():\n"
        "    models.Item()\n",
    )
    graph = build_code_graph(scan_project(workspace))
    service_id = _node_id(graph, "pkg.service")
    models_id = _node_id(graph, "pkg.models")
    assert models_id in _edge_targets(graph, service_id, "imports")
    item_id = _node_id(graph, "pkg.models.Item")
    use_id = _node_id(graph, "pkg.service.use")
    assert item_id in _edge_targets(graph, use_id, "calls")


def test_codegraph_duplicate_symbol_ids_are_avoided(tmp_path: Path) -> None:
    """property setter 等同名定义不能导致图构建失败。"""
    workspace, root = _workspace(tmp_path)
    _write(
        root,
        "model.py",
        '''class Model:
    @property
    def value(self):
        return 1

    @value.setter
    def value(self, new_value):
        self._value = new_value
''',
    )
    graph = build_code_graph(scan_project(workspace))
    value_nodes = [
        node for node in graph.nodes.values() if node.name == "value"
    ]
    assert len(value_nodes) == 2
    assert len({node.id for node in value_nodes}) == 2
