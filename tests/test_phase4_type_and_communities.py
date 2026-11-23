"""Phase 4 单元测试：Python 静态类型/继承调用解析、确定性社区聚类与架构 Prompt 压降。"""

from __future__ import annotations

from pathlib import Path

from mini_harness.architecture import (
    ArchitectureInputBuilder,
    build_architecture_prompt,
    detect_architecture_communities,
    is_noise_path,
)
from mini_harness.explorer import build_code_graph, scan_project
from mini_harness.workspace import WorkspaceResolver


def _write(root: Path, rel_path: str, content: str) -> None:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _call_targets_for(graph, caller_qname: str) -> set[str]:
    caller = next(
        node for node in graph.nodes.values() if node.qualified_name == caller_qname
    )
    targets: set[str] = set()
    for edge in graph.edges:
        if edge.kind == "calls" and edge.source == caller.id:
            target_node = graph.get_node(edge.target)
            if target_node is not None:
                targets.add(target_node.qualified_name)
    return targets


def test_python_type_annotations_and_self_attr_resolve_calls(tmp_path: Path) -> None:
    """参数注解、Optional/Union 注解、self.attr 与直接构造赋值应解析为静态 calls 边。"""
    _write(tmp_path, "app/__init__.py", "")
    _write(
        tmp_path,
        "app/repo.py",
        (
            "class UserRepo:\n"
            "    def fetch(self, uid: str) -> str:\n"
            "        return uid\n\n"
            "    def save(self, uid: str) -> None:\n"
            "        pass\n"
        ),
    )
    _write(
        tmp_path,
        "app/cache.py",
        (
            "class Cache:\n"
            "    def invalidate(self, key: str) -> None:\n"
            "        pass\n"
        ),
    )
    _write(
        tmp_path,
        "app/service.py",
        (
            "from typing import Optional\n"
            "from .cache import Cache\n"
            "from .repo import UserRepo\n\n"
            "def make_dynamic():\n"
            "    return UserRepo()\n\n"
            "class UserService:\n"
            "    shared_cache: Cache\n\n"
            "    def __init__(self, repo: UserRepo, opt_cache: Optional[Cache] = None) -> None:\n"
            "        self.repo = repo\n"
            "        self.local_cache = Cache()\n"
            "        self.opt_cache = opt_cache\n\n"
            "    def update_user(self, uid: str, extra: UserRepo | None = None) -> str:\n"
            "        val = self.repo.fetch(uid)\n"
            "        self.local_cache.invalidate(uid)\n"
            "        if self.opt_cache is not None:\n"
            "            self.opt_cache.invalidate(uid)\n"
            "        if extra is not None:\n"
            "            extra.save(uid)\n"
            "        direct = UserRepo()\n"
            "        direct.save(uid)\n"
            "        dyn = make_dynamic()\n"
            "        dyn.fetch(uid)\n"
            "        return val\n"
        ),
    )

    ws = WorkspaceResolver(cwd=tmp_path).resolve(tmp_path)
    index = scan_project(ws)
    graph = build_code_graph(index)

    targets = _call_targets_for(graph, "app.service.UserService.update_user")
    assert "app.repo.UserRepo.fetch" in targets
    assert "app.repo.UserRepo.save" in targets
    assert "app.cache.Cache.invalidate" in targets
    assert "app.service.make_dynamic" in targets

    # 未标注工厂返回值 dyn = make_dynamic(); dyn.fetch(uid) 必须保持 unresolved，绝不伪造静态边
    caller = next(
        n for n in graph.nodes.values() if n.qualified_name == "app.service.UserService.update_user"
    )
    unresolved_targets = {
        u.expression for u in graph.unresolved if u.kind == "call" and u.source_id == caller.id
    }
    assert "dyn.fetch" in unresolved_targets


def test_python_inheritance_and_mixin_method_resolution(tmp_path: Path) -> None:
    """子类通过 self 或类型注解调用父类 / Mixin 方法应沿继承链解析，且对循环继承安全。"""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/base.py",
        (
            "class AuditMixin:\n"
            "    def record_audit(self, msg: str) -> None:\n"
            "        pass\n\n"
            "class BaseRunner:\n"
            "    def prepare(self) -> None:\n"
            "        pass\n"
        ),
    )
    _write(
        tmp_path,
        "pkg/impl.py",
        (
            "from .base import AuditMixin, BaseRunner\n\n"
            "class CustomRunner(AuditMixin, BaseRunner):\n"
            "    def execute(self) -> None:\n"
            "        self.prepare()\n"
            "        self.record_audit('done')\n\n"
            "def run_all(runner: CustomRunner) -> None:\n"
            "    runner.prepare()\n"
            "    runner.record_audit('outer')\n"
        ),
    )
    # 极端情况：非法循环继承不能导致死循环
    _write(
        tmp_path,
        "pkg/cycle.py",
        (
            "class LoopA(LoopB):\n"
            "    def f(self) -> None:\n"
            "        self.missing_method()\n\n"
            "class LoopB(LoopA):\n"
            "    pass\n"
        ),
    )

    ws = WorkspaceResolver(cwd=tmp_path).resolve(tmp_path)
    index = scan_project(ws)
    graph = build_code_graph(index)

    exec_targets = _call_targets_for(graph, "pkg.impl.CustomRunner.execute")
    assert "pkg.base.BaseRunner.prepare" in exec_targets
    assert "pkg.base.AuditMixin.record_audit" in exec_targets

    outer_targets = _call_targets_for(graph, "pkg.impl.run_all")
    assert "pkg.base.BaseRunner.prepare" in outer_targets
    assert "pkg.base.AuditMixin.record_audit" in outer_targets


def test_community_detection_is_deterministic_and_excludes_noise(tmp_path: Path) -> None:
    """确定性社区聚类应将耦合子包聚成子系统、过滤 tests/scripts 噪声并输出跨社区链路。"""
    _write(tmp_path, "core/__init__.py", "")
    _write(
        tmp_path,
        "core/engine.py",
        (
            "from storage.db import Database\n\n"
            "class Engine:\n"
            "    def __init__(self, db: Database) -> None:\n"
            "        self.db = db\n\n"
            "    def run(self) -> str:\n"
            "        return self.db.query()\n"
        ),
    )
    _write(
        tmp_path,
        "core/planner.py",
        (
            "from .engine import Engine\n\n"
            "def plan(engine: Engine) -> str:\n"
            "    return engine.run()\n"
        ),
    )
    _write(tmp_path, "storage/__init__.py", "")
    _write(
        tmp_path,
        "storage/db.py",
        (
            "class Database:\n"
            "    def query(self) -> str:\n"
            "        return 'ok'\n"
        ),
    )
    _write(
        tmp_path,
        "storage/migrations.py",
        (
            "from .db import Database\n\n"
            "def migrate(db: Database) -> str:\n"
            "    return db.query()\n"
        ),
    )
    # 噪声脚本与测试文件
    _write(
        tmp_path,
        "scripts/bench.py",
        (
            "def main() -> None:\n"
            "    pass\n"
        ),
    )
    _write(
        tmp_path,
        "tests/test_engine.py",
        (
            "def main() -> None:\n"
            "    pass\n"
        ),
    )

    assert is_noise_path("scripts/bench.py") is True
    assert is_noise_path("tests/test_engine.py") is True
    assert is_noise_path("frontend/src/App.test.tsx") is True
    assert is_noise_path("core/engine.py") is False

    ws = WorkspaceResolver(cwd=tmp_path).resolve(tmp_path)
    index = scan_project(ws)
    graph = build_code_graph(index)

    c1 = detect_architecture_communities(graph)
    c2 = detect_architecture_communities(graph)
    assert [c.to_dict() for c in c1] == [c.to_dict() for c in c2]
    assert len(c1) >= 2

    all_modules = {m for c in c1 for m in c.modules}
    assert "scripts/bench.py" not in all_modules
    assert "tests/test_engine.py" not in all_modules
    assert "core/engine.py" in all_modules
    assert "storage/db.py" in all_modules
    # 存在跨社区链路
    assert any(len(c.external_links) > 0 for c in c1)


def test_architecture_input_denoises_entries_and_compact_prompt_embeds_communities(
    tmp_path: Path,
) -> None:
    """ArchitectureInputBuilder 应剔除 scripts 噪声入口、正确统计 unresolved calls，并输出含社区摘要的紧凑 Prompt。"""
    _write(tmp_path, "web/__init__.py", "")
    _write(
        tmp_path,
        "web/app.py",
        (
            "from service.worker import Worker\n\n"
            "def create_app() -> Worker:\n"
            "    w = Worker()\n"
            "    w.start()\n"
            "    return w\n"
        ),
    )
    _write(tmp_path, "service/__init__.py", "")
    _write(
        tmp_path,
        "service/worker.py",
        (
            "def get_ext():\n"
            "    return None\n\n"
            "class Worker:\n"
            "    def start(self) -> None:\n"
            "        ext = get_ext()\n"
            "        ext.ping()\n"
        ),
    )
    _write(
        tmp_path,
        "scripts/offline_eval.py",
        (
            "def main() -> None:\n"
            "    pass\n"
        ),
    )

    ws = WorkspaceResolver(cwd=tmp_path).resolve(tmp_path)
    index = scan_project(ws)
    graph = build_code_graph(index)

    analysis_input = ArchitectureInputBuilder(index, graph).build()
    entry_files = [e.symbol.file_path for e in analysis_input.entry_points]
    assert "web/app.py" in entry_files
    assert "scripts/offline_eval.py" not in entry_files
    assert analysis_input.communities
    # unresolvedSummary["calls"] 应准确统计 unresolved call（修复前恒为 0）
    assert analysis_input.unresolved_summary["calls"] >= 1

    prompt = build_architecture_prompt(analysis_input, max_input_tokens=32000)
    assert prompt.trimmed is False
    assert "communities" in prompt.payload
    assert len(prompt.payload["communities"]) >= 1
    assert "communities" in prompt.system_prompt
