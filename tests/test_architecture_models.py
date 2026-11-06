"""Phase 4.2 ArchitectureGraph 模型与源码引用协议测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_harness.architecture import (
    ArchitectureEdge,
    ArchitectureGraph,
    ArchitectureGraphValidator,
    ArchitectureNode,
    ArchitectureReferenceError,
    ArchitectureSourceRef,
    ArchitectureValidationReport,
    ArchitectureView,
    SourceRefProtocol,
    SymbolIndex,
)
from mini_harness.explorer import build_code_graph, scan_project
from mini_harness.workspace import WorkspaceResolver


def _project(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(
        '''from pkg.loop import AgentLoop\n\n\ndef main() -> None:\n    loop = AgentLoop()\n    loop.run()\n''',
        encoding="utf-8",
    )
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "loop.py").write_text(
        '''class AgentLoop:\n    """Message loop."""\n\n    def run(self) -> None:\n        self._dispatch()\n\n    def _dispatch(self) -> None:\n        pass\n''',
        encoding="utf-8",
    )
    workspace = WorkspaceResolver(cwd=tmp_path).resolve(root)
    index = scan_project(workspace)
    return workspace, index, build_code_graph(index)


def _graph_with_ref(symbol_id: str) -> ArchitectureGraph:
    return ArchitectureGraph(
        project_name="project",
        project_root="/tmp/project",
        project_description="test",
        workspace_root="/tmp/project",
        workspace_id="test",
        generated_at="2026-01-01T00:00:00+00:00",
        views=[
            ArchitectureView(
                id="overview",
                title="Overview",
                nodes=[
                    ArchitectureNode(
                        id="loop",
                        kind="component",
                        label="AgentLoop",
                        description="Loop",
                        source_refs=[ArchitectureSourceRef(symbol_id=symbol_id)],
                    )
                ],
            )
        ],
    )


def test_symbol_index_resolves_ids_names_and_locations(tmp_path: Path) -> None:
    _, _, graph = _project(tmp_path)
    index = SymbolIndex.from_code_graph(graph)
    symbol = next(
        item
        for item in graph.nodes.values()
        if item.qualified_name == "pkg.loop.AgentLoop.run"
    )

    assert index.get(symbol.id) is symbol
    assert index.find_by_qualified_name("pkg.loop.AgentLoop.run") == [symbol]
    assert index.find_at(symbol.file_path, symbol.start_line) is not None
    assert index.find_at(symbol.file_path, 1) is not None


def test_source_ref_protocol_canonicalizes_and_rejects_invalid_refs(tmp_path: Path) -> None:
    _, _, graph = _project(tmp_path)
    protocol = SourceRefProtocol(SymbolIndex.from_code_graph(graph))
    symbol = next(
        item
        for item in graph.nodes.values()
        if item.qualified_name == "pkg.loop.AgentLoop.run"
    )

    resolved = protocol.resolve(ArchitectureSourceRef(symbol_id=symbol.id))
    assert resolved.verified is True
    assert resolved.file_path == symbol.file_path
    assert resolved.start_line == symbol.start_line
    assert resolved.end_line == symbol.end_line
    assert resolved.qualified_name == symbol.qualified_name

    with pytest.raises(ArchitectureReferenceError):
        protocol.resolve(
            ArchitectureSourceRef(
                symbol_id="method:pkg.loop.AgentLoop.does_not_exist"
            )
        )

    with pytest.raises(ArchitectureReferenceError):
        protocol.resolve(
            ArchitectureSourceRef(
                symbol_id=symbol.id,
                file_path="pkg/other.py",
            )
        )

    # 即使 qualifiedName 正确，只要显式 symbolId 是虚构的也必须拒绝。
    with pytest.raises(ArchitectureReferenceError):
        protocol.resolve(
            ArchitectureSourceRef(
                symbol_id="method:pkg.loop.AgentLoop.invented",
                qualified_name=symbol.qualified_name,
            )
        )


def test_architecture_graph_roundtrip_keeps_source_refs_and_views() -> None:
    graph = _graph_with_ref("method:pkg.loop.AgentLoop.run")
    payload = graph.to_dict()

    assert payload["schemaVersion"] == 1
    assert payload["views"]["overview"]["nodes"][0]["sourceRefs"][0]["symbolId"] == (
        "method:pkg.loop.AgentLoop.run"
    )

    restored = ArchitectureGraph.from_dict(payload)
    assert restored.project_name == graph.project_name
    assert restored.default_view_id == "overview"
    assert restored.views[0].nodes[0].source_refs[0].symbol_id == (
        "method:pkg.loop.AgentLoop.run"
    )


def test_graph_validator_canonicalizes_refs_and_downgrades_unproven_edges(
    tmp_path: Path,
) -> None:
    _, _, graph_index = _project(tmp_path)
    graph = _graph_with_ref("method:pkg.loop.AgentLoop.run")
    view = graph.views[0]
    # 构造一个最小的合法图，再将边单独交给 validator。
    # ArchitectureEdge 禁止自环，因此换成两个节点。
    node_a = view.nodes[0]
    node_b = ArchitectureNode(id="runner", kind="component", label="Runner")
    graph_with_edge = ArchitectureGraph(
        project_name=graph.project_name,
        project_root=graph.project_root,
        project_description=graph.project_description,
        workspace_root=graph.workspace_root,
        workspace_id=graph.workspace_id,
        generated_at=graph.generated_at,
        views=[
            ArchitectureView(
                id="overview",
                title="Overview",
                nodes=[node_a, node_b],
                edges=[
                    ArchitectureEdge(
                        id="loop-flow",
                        source="loop",
                        target="runner",
                        kind="flow",
                        confidence="static",
                    )
                ],
            )
        ],
    )

    report: ArchitectureValidationReport = ArchitectureGraphValidator(
        SymbolIndex.from_code_graph(graph_index)
    ).check(graph_with_edge)
    assert report.ok is True
    assert report.graph.views[0].nodes[0].source_refs[0].verified is True
    assert report.graph.views[0].edges[0].confidence == "inferred"
    assert any(issue.code == "static_without_evidence" for issue in report.issues)

    invalid_graph = _graph_with_ref("method:pkg.loop.AgentLoop.missing")
    with pytest.raises(ArchitectureReferenceError):
        ArchitectureGraphValidator(
            SymbolIndex.from_code_graph(graph_index)
        ).validate(invalid_graph)


def test_phase_4_1_frontend_mock_is_loadable_by_backend_model() -> None:
    root = Path(__file__).resolve().parents[1]
    raw = json.loads(
        (root / "frontend" / "src" / "mocks" / "architecture-graph.json").read_text(
            encoding="utf-8"
        )
    )
    graph = ArchitectureGraph.from_dict(raw)

    assert graph.mock is True
    assert graph.default_view_id == "overview"
    assert set(graph.views_by_id) == {"overview", "agent-loop", "agent-runner"}
    assert graph.views_by_id["overview"].nodes[2].nested[0].label == "AgentRunner"
