"""Phase 1：SQLite WAL 增量索引、FTS5+CodeGraph 混合检索与 search_code 工具测试。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_harness.explorer import (
    SQLiteIndexStore,
    build_code_graph,
    scan_project,
    tokenize_for_fts,
)
from mini_harness.tools import SearchCodeTool, ToolRegistry
from mini_harness.web import create_app
from mini_harness.workspace import WorkspaceResolver


def _make_sample_workspace(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text('"""Core package."""\n', encoding="utf-8")
    (root / "pkg" / "governor.py").write_text(
        '"""上下文预算治理与自动压缩模块。"""\n\n'
        "class ContextGovernor:\n"
        '    """检查输入预算，超限时触发压缩与确定性回退。"""\n\n'
        "    def prepare_request(self, messages: list[str]) -> list[str]:\n"
        '        """校验预算并在必要时执行上下文裁剪。"""\n'
        "        return self._fallback_trim(messages)\n\n"
        "    def _fallback_trim(self, messages: list[str]) -> list[str]:\n"
        '        """确定性回退裁剪旧轮次。"""\n'
        "        return messages[-4:]\n",
        encoding="utf-8",
    )
    (root / "pkg" / "runner.py").write_text(
        "from .governor import ContextGovernor\n\n"
        "def run_agent(messages: list[str]) -> list[str]:\n"
        '    """执行代理主循环并调用预算治理。"""\n'
        "    gov = ContextGovernor()\n"
        "    return gov.prepare_request(messages)\n",
        encoding="utf-8",
    )
    workspace = WorkspaceResolver(cwd=tmp_path).resolve(root)
    return workspace, root


def test_tokenize_for_fts_handles_camelcase_snakecase_and_cjk() -> None:
    tokens = tokenize_for_fts("ContextGovernor.prepare_request 上下文压缩")
    assert "contextgovernor" in tokens
    assert "context" in tokens
    assert "governor" in tokens
    assert "prepare" in tokens
    assert "request" in tokens
    assert "上下" in tokens
    assert "下文" in tokens
    assert "压缩" in tokens


def test_sqlite_incremental_sync_lifecycle(tmp_path: Path) -> None:
    workspace, root = _make_sample_workspace(tmp_path)
    store = SQLiteIndexStore(workspace, base_dir=tmp_path / "db_home")

    # 1. Cold sync 与全量扫描结果一致
    cold = store.sync()
    baseline_idx = scan_project(workspace)
    baseline_graph = build_code_graph(baseline_idx)

    assert cold.stats.total_files == 3
    assert cold.stats.added_files == 3
    assert cold.stats.reparsed_files == 3
    assert cold.stats.edges_rebuilt is True
    assert len(cold.index.symbols) == len(baseline_idx.symbols)
    assert len(cold.graph.edges) == len(baseline_graph.edges)

    # 2. Warm 无变更同步：0 文件重解析，不重建边
    warm = store.sync()
    assert warm.stats.unchanged_files == 3
    assert warm.stats.reparsed_files == 0
    assert warm.stats.edges_rebuilt is False

    # 3. 仅 touch mtime 但内容不变：通过 sha256 跳过重解析
    runner_path = root / "pkg" / "runner.py"
    future_ns = runner_path.stat().st_mtime_ns + 5_000_000_000
    os.utime(runner_path, ns=(future_ns, future_ns))
    touched = store.sync()
    assert touched.stats.reparsed_files == 0
    assert touched.stats.unchanged_files == 3

    # 4. 修改单个文件：只重解析这 1 个文件
    runner_path.write_text(
        runner_path.read_text(encoding="utf-8")
        + "\ndef helper_ping() -> str:\n    return 'pong'\n",
        encoding="utf-8",
    )
    updated = store.sync()
    assert updated.stats.reparsed_files == 1
    assert updated.stats.updated_files == 1
    assert updated.stats.unchanged_files == 2
    assert any(s.name == "helper_ping" for s in updated.index.symbols)

    # 5. 删除文件：自动清理符号、FTS 与边
    runner_path.unlink()
    after_delete = store.sync()
    assert after_delete.stats.deleted_files == 1
    assert after_delete.stats.total_files == 2
    assert not any(s.name == "helper_ping" for s in after_delete.index.symbols)
    assert store.search("helper_ping") == []


def test_sqlite_hybrid_search_chinese_camel_and_neighbors(tmp_path: Path) -> None:
    workspace, _ = _make_sample_workspace(tmp_path)
    store = SQLiteIndexStore(workspace, base_dir=tmp_path / "db_home")
    store.sync()

    # 中文概念查询
    cn_hits = store.search("上下文 压缩 回退", limit=5)
    assert cn_hits
    assert cn_hits[0].file_path == "pkg/governor.py"

    # 驼峰拆词 + kind 过滤
    class_hits = store.search("governor", kind="class", limit=5)
    assert len(class_hits) == 1
    assert class_hits[0].name == "ContextGovernor"

    # path_prefix 过滤
    runner_hits = store.search("run_agent", path_prefix="pkg/runner.py")
    assert runner_hits
    assert all(h.file_path == "pkg/runner.py" for h in runner_hits)

    # 验证直接调用关系返回
    trim_hits = store.search("_fallback_trim", kind="method")
    assert trim_hits
    assert any("prepare_request" in caller for caller in trim_hits[0].callers)


@pytest.mark.asyncio
async def test_search_code_tool_via_registry(tmp_path: Path) -> None:
    workspace, _ = _make_sample_workspace(tmp_path)
    tool = SearchCodeTool(workspace, base_dir=tmp_path / "db_home")
    registry = ToolRegistry()
    registry.register(tool)

    result = await registry.execute(
        "search_code",
        {"query": "上下文 预算 回退", "limit": 3},
    )
    assert result.success
    assert "pkg/governor.py" in result.content
    assert "ContextGovernor" in result.content

    # 路径越界被拦截
    bad = await registry.execute(
        "search_code",
        {"query": "governor", "path_prefix": "../outside"},
    )
    assert bad.is_error


def test_web_app_sqlite_incremental_and_search_endpoint(tmp_path: Path) -> None:
    workspace, _ = _make_sample_workspace(tmp_path)
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>Explorer</h1>", encoding="utf-8")
    db_path = tmp_path / "explorer.db"

    app = create_app(
        workspace,
        frontend_dir=frontend,
        index_db_path=db_path,
        use_sqlite_cache=True,
    )
    with TestClient(app) as client:
        project = client.get("/api/project")
        assert project.status_code == 200
        assert project.json()["syncStats"]["totalFiles"] == 3
        assert project.json()["syncStats"]["reparsedFiles"] == 3

        search_resp = client.get("/api/search", params={"q": "上下文 压缩", "limit": 5})
        assert search_resp.status_code == 200
        payload = search_resp.json()
        assert payload["count"] >= 1
        assert payload["hits"][0]["filePath"] == "pkg/governor.py"

        # 越界 path_prefix 返回 400
        escape_resp = client.get(
            "/api/search",
            params={"q": "governor", "path_prefix": "../../etc"},
        )
        assert escape_resp.status_code == 400

    # 二次创建应用复用同一 SQLite 缓存：0 文件重解析
    app2 = create_app(
        workspace,
        frontend_dir=frontend,
        index_db_path=db_path,
        use_sqlite_cache=True,
    )
    with TestClient(app2) as client:
        project2 = client.get("/api/project").json()
        assert project2["syncStats"]["reparsedFiles"] == 0
        assert project2["syncStats"]["unchangedFiles"] == 3
