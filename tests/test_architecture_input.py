"""Phase 4.2 CodeGraph → 语义架构分析输入转换测试。"""

from __future__ import annotations

import json
from pathlib import Path

from mini_harness.architecture import (
    ArchitectureInputBuilder,
    ArchitectureRelationSummary,
    ArchitectureSymbolSummary,
    build_architecture_input,
)
from mini_harness.explorer import build_code_graph, scan_project
from mini_harness.workspace import WorkspaceResolver


def _fixture_project(tmp_path: Path):
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
    (pkg / "runner.py").write_text(
        '''class AgentRunner:\n    """Runner."""\n\n    def run(self) -> None:\n        pass\n''',
        encoding="utf-8",
    )
    workspace = WorkspaceResolver(cwd=tmp_path).resolve(root)
    index = scan_project(workspace)
    return index, build_code_graph(index)


def test_input_builder_selects_entry_points_and_key_symbols(tmp_path: Path) -> None:
    index, graph = _fixture_project(tmp_path)
    data = ArchitectureInputBuilder(index, graph).build()

    entry_names = {item.symbol.qualified_name for item in data.entry_points}
    assert any(name.endswith(".main") for name in entry_names)
    assert all(item.reasons for item in data.entry_points)
    assert any(
        item.qualified_name == "pkg.loop.AgentLoop"
        for item in data.key_symbols
    )
    assert any(item.qualified_name == "pkg.loop.AgentLoop.run" for item in data.key_symbols)


def test_input_builder_keeps_relations_and_compact_sources(tmp_path: Path) -> None:
    index, graph = _fixture_project(tmp_path)
    data = build_architecture_input(
        index,
        graph,
        max_source_excerpts=2,
        max_excerpt_lines=3,
        max_excerpt_chars=300,
    )

    assert isinstance(data.modules[0], ArchitectureSymbolSummary)
    assert isinstance(data.imports[0], ArchitectureRelationSummary)
    assert len(data.source_excerpts) <= 2
    assert data.unresolved_summary["total"] == len(graph.unresolved)
    for excerpt in data.source_excerpts:
        assert excerpt.end_line - excerpt.start_line + 1 <= 3
        assert len(excerpt.text) <= 300
    assert sum(len(item.text) for item in data.source_excerpts) <= 600


def test_input_payload_is_json_serializable_and_declares_protocol(tmp_path: Path) -> None:
    index, graph = _fixture_project(tmp_path)
    data = ArchitectureInputBuilder(index, graph).build()
    payload = data.to_dict()

    json.dumps(payload, ensure_ascii=False)
    assert payload["schemaVersion"] == 1
    assert payload["project"]["name"] == "project"
    assert payload["sourceReferenceProtocol"]["version"] == 1
    assert "symbolId" in payload["sourceReferenceProtocol"]["referenceFields"]
    assert payload["graphStats"]["nodes"] == len(graph.nodes)
    assert payload["sourceExcerpts"]


def test_input_builder_respects_relation_caps(tmp_path: Path) -> None:
    index, graph = _fixture_project(tmp_path)
    data = ArchitectureInputBuilder(
        index,
        graph,
        max_import_relations=1,
        max_call_relations=1,
    ).build()

    assert len(data.imports) <= 1
    assert len(data.calls) <= 1
    assert data.limits["maxImportRelations"] == 1
    assert data.limits["maxCallRelations"] == 1


def test_input_builder_can_consume_code_graph_only(tmp_path: Path) -> None:
    _, graph = _fixture_project(tmp_path)
    data = ArchitectureInputBuilder.from_graph(graph, max_source_excerpts=1)

    assert data.generated_at == ""
    assert data.workspace_root == graph.workspace_root
    assert len(data.source_excerpts) <= 1
