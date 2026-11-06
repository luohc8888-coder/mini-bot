"""ProjectIndex 与 IndexStore 测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mini_harness.explorer import (
    IndexStore,
    IndexStoreError,
    ProjectIndex,
    SCHEMA_VERSION,
    scan_project,
)
from mini_harness.workspace import WorkspaceResolver


def _workspace(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    return WorkspaceResolver(cwd=tmp_path).resolve(root), root


def test_index_store_round_trip(tmp_path: Path) -> None:
    """索引保存后应能完整读回，并位于 Workspace 自己的目录下。"""
    workspace, root = _workspace(tmp_path)
    (root / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    index = scan_project(workspace)
    store = IndexStore(workspace, base_dir=tmp_path / "explorer-index")

    assert store.load() is None
    path = store.save(index)
    assert path == tmp_path / "explorer-index" / workspace.id / "index.json"
    assert path.is_file()

    loaded = store.load()
    assert loaded is not None
    assert loaded.stats == index.stats
    assert [item.to_dict() for item in loaded.symbols] == [
        item.to_dict() for item in index.symbols
    ]
    assert [item.to_dict() for item in loaded.calls] == [
        item.to_dict() for item in index.calls
    ]


def test_index_store_rejects_workspace_mismatch(tmp_path: Path) -> None:
    """不能把 A Workspace 的索引写到 B Workspace 的目录。"""
    workspace, root = _workspace(tmp_path)
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    index = scan_project(workspace)
    mismatched = replace(index, workspace_id="another-workspace")
    store = IndexStore(workspace, base_dir=tmp_path / "explorer-index")

    with pytest.raises(IndexStoreError, match="workspace_id"):
        store.save(mismatched)


def test_project_index_defaults_and_schema() -> None:
    """ProjectIndex 的缺省 schema 和空统计应保持一致。"""
    index = ProjectIndex(
        schema_version=SCHEMA_VERSION,
        workspace_root="/tmp/project",
        workspace_id="abc123",
        generated_at="2026-01-01T00:00:00+00:00",
    )
    assert index.stats["files"] == 0
    assert index.stats["classes"] == 0
    assert index.to_dict()["schemaVersion"] == SCHEMA_VERSION


def test_index_store_rejects_unknown_schema(tmp_path: Path) -> None:
    """schemaVersion 不匹配时不能静默加载。"""
    workspace, _ = _workspace(tmp_path)
    store = IndexStore(workspace, base_dir=tmp_path / "explorer-index")
    store.index_path.parent.mkdir(parents=True)
    store.index_path.write_text(
        '{"schemaVersion": 999, "workspaceId": "x"}',
        encoding="utf-8",
    )
    with pytest.raises(IndexStoreError, match="schemaVersion"):
        store.load()
