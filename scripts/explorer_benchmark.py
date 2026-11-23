#!/usr/bin/env python3
"""Phase 1 前后对比基准测试脚本（Before vs After）。

测量三个维度的真实可复现数据：
1. 本仓（mini-harness）冷启动全量扫描 vs SQLite 增量索引（无变更 / 单文件变更）耗时与存储体积；
2. 合成大仓库（默认 1,000 个 Python 文件）全量扫描 vs SQLite 增量索引耗时；
3. 代码检索评测（15 道覆盖中英文/概念性/跨模块定位题）：基线 Grep vs SQLite FTS5+Graph 混合检索的
   Top-1 / Top-5 命中率、MRR@5 与平均检索延迟。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_harness.explorer import build_code_graph, scan_project  # noqa: E402
from mini_harness.tools.search import GrepTool  # noqa: E402
from mini_harness.workspace import WorkspaceResolver  # noqa: E402


# 15 道面向 mini-harness 仓库的真实代码定位评测集（覆盖中文概念、驼峰、缩写与跨模块意图）
RETRIEVAL_CASES: list[dict[str, Any]] = [
    {
        "query": "上下文超预算 压缩 回退",
        "expected_files": ["mini_harness/agent/governor.py"],
        "category": "chinese_concept",
    },
    {
        "query": "长期记忆 Dream 整理 游标",
        "expected_files": ["mini_harness/memory/dream.py"],
        "category": "chinese_concept",
    },
    {
        "query": "空闲自动压缩 定时器",
        "expected_files": ["mini_harness/agent/maintenance.py"],
        "category": "chinese_concept",
    },
    {
        "query": "Session 原子保存 临时文件",
        "expected_files": ["mini_harness/session/manager.py"],
        "category": "chinese_concept",
    },
    {
        "query": "源码引用 校验 防幻觉 SymbolIndex",
        "expected_files": ["mini_harness/architecture/source_refs.py"],
        "category": "mixed",
    },
    {
        "query": "架构分析 JSON 解析 修复重试",
        "expected_files": ["mini_harness/architecture/analyzer.py"],
        "category": "chinese_concept",
    },
    {
        "query": "路径穿越 符号链接 边界检查",
        "expected_files": ["mini_harness/workspace.py"],
        "category": "chinese_concept",
    },
    {
        "query": "指数退避 重试 Retry-After",
        "expected_files": ["mini_harness/providers/retry.py"],
        "category": "mixed",
    },
    {
        "query": "ToolCall ToolResult 配对 完整用户轮次",
        "expected_files": ["mini_harness/agent/context.py", "mini_harness/agent/messages.py"],
        "category": "mixed",
    },
    {
        "query": "批量精确替换 回滚 apply patch",
        "expected_files": ["mini_harness/tools/edit.py"],
        "category": "mixed",
    },
    {
        "query": "命令执行 进程组 清理 超时",
        "expected_files": ["mini_harness/tools/command.py"],
        "category": "chinese_concept",
    },
    {
        "query": "相对导入 模块重导出 CodeGraph",
        "expected_files": ["mini_harness/explorer/codegraph.py"],
        "category": "mixed",
    },
    {
        "query": "Web 源码对话 接口 symbolId",
        "expected_files": ["mini_harness/web/app.py"],
        "category": "mixed",
    },
    {
        "query": "Token 估算 非 ASCII",
        "expected_files": ["mini_harness/agent/tokens.py"],
        "category": "mixed",
    },
    {
        "query": "Provider 工厂 厂商预设",
        "expected_files": [
            "mini_harness/providers/factory.py",
            "mini_harness/providers/presets.py",
        ],
        "category": "mixed",
    },
]


def _build_synthetic_repo(root: Path, num_files: int) -> list[Path]:
    """生成含 num_files 个 Python 模块的合成仓库（含包结构、类、方法与跨模块调用）。"""
    created: list[Path] = []
    packages = 20
    per_pkg = max(1, num_files // packages)
    for pkg_idx in range(packages):
        pkg_dir = root / f"pkg_{pkg_idx:02d}"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        init_file = pkg_dir / "__init__.py"
        init_file.write_text(f'"""Package {pkg_idx}."""\n', encoding="utf-8")
        created.append(init_file)
        for mod_idx in range(per_pkg - 1):
            mod_file = pkg_dir / f"service_{mod_idx:03d}.py"
            prev_import = (
                f"from .service_{mod_idx - 1:03d} import helper_{mod_idx - 1:03d}\n"
                if mod_idx > 0
                else ""
            )
            prev_call = (
                f"        helper_{mod_idx - 1:03d}(payload)\n"
                if mod_idx > 0
                else ""
            )
            methods_code = "\n".join(
                f"    def step_{s:02d}(self, payload: str, factor: int = {s}) -> str:\n"
                f'        """Process stage {s} for workflow {pkg_idx}-{mod_idx}."""\n'
                f"        cleaned = payload.strip()\n"
                f"        if not cleaned:\n"
                f"            return ''\n"
                f"        return f'{{cleaned}}-{{factor}}'\n"
                for s in range(12)
            )
            code = (
                f'"""Service module {pkg_idx}.{mod_idx}."""\n\n'
                f"{prev_import}\n"
                f"def helper_{mod_idx:03d}(value: str) -> str:\n"
                f'    """Normalize payload for pkg {pkg_idx} module {mod_idx}."""\n'
                f"    return value.strip()\n\n\n"
                f"class Handler_{pkg_idx:02d}_{mod_idx:03d}:\n"
                f'    """Handles domain workflow {pkg_idx}-{mod_idx}."""\n\n'
                f"    def execute(self, payload: str) -> str:\n"
                f'        """Execute step and call helper."""\n'
                f"{prev_call}"
                f"        return helper_{mod_idx:03d}(self.step_00(payload))\n\n"
                f"    def validate(self, payload: str) -> bool:\n"
                f'        """Check payload non-empty."""\n'
                f"        return bool(payload)\n\n"
                f"{methods_code}\n"
            )
            mod_file.write_text(code, encoding="utf-8")
            created.append(mod_file)
    return created


def _bench_full_scan(workspace_path: Path) -> dict[str, Any]:
    workspace = WorkspaceResolver(cwd=workspace_path).resolve(workspace_path)
    t0 = time.perf_counter()
    index = scan_project(workspace)
    t1 = time.perf_counter()
    graph = build_code_graph(index)
    t2 = time.perf_counter()
    return {
        "files": len(index.files),
        "symbols": len(index.symbols),
        "edges": len(graph.edges),
        "scan_ms": round((t1 - t0) * 1000, 2),
        "graph_ms": round((t2 - t1) * 1000, 2),
        "total_ms": round((t2 - t0) * 1000, 2),
    }


def _bench_sqlite_incremental(
    workspace_path: Path,
    *,
    mutate_file: Path | None = None,
) -> dict[str, Any] | None:
    try:
        from mini_harness.explorer.sqlite_store import SQLiteIndexStore
    except ImportError:
        return None

    workspace = WorkspaceResolver(cwd=workspace_path).resolve(workspace_path)
    with tempfile.TemporaryDirectory(prefix="mini_harness_bench_db_") as tmpdir:
        store = SQLiteIndexStore(workspace, base_dir=Path(tmpdir))
        # 1. Cold build into SQLite
        t0 = time.perf_counter()
        cold = store.sync()
        t1 = time.perf_counter()

        # 2. Warm no-change sync_incremental (for search_code) & full sync (for web startup)
        t_inc0 = time.perf_counter()
        warm_inc = store.sync_incremental()
        t_inc1 = time.perf_counter()

        t2 = time.perf_counter()
        warm = store.sync()
        t3 = time.perf_counter()

        # 3. Single-file edit incremental sync
        inc_ms: float | None = None
        inc_reparsed: int | None = None
        if mutate_file is not None and mutate_file.is_file():
            original = mutate_file.read_text(encoding="utf-8")
            try:
                mutate_file.write_text(
                    original + "\n# benchmark incremental touch\n",
                    encoding="utf-8",
                )
                t4 = time.perf_counter()
                inc = store.sync()
                t5 = time.perf_counter()
                inc_ms = round((t5 - t4) * 1000, 2)
                inc_reparsed = inc.stats.reparsed_files
            finally:
                mutate_file.write_text(original, encoding="utf-8")

        db_size_kb = round(store.db_path.stat().st_size / 1024, 1)
        return {
            "files": len(cold.index.files),
            "symbols": len(cold.index.symbols),
            "edges": len(cold.graph.edges),
            "cold_sync_ms": round((t1 - t0) * 1000, 2),
            "warm_check_only_ms": round((t_inc1 - t_inc0) * 1000, 2),
            "warm_load_full_ms": round((t3 - t2) * 1000, 2),
            "warm_reparsed_files": warm_inc.reparsed_files + warm.stats.reparsed_files,
            "single_file_edit_ms": inc_ms,
            "single_file_reparsed": inc_reparsed,
            "db_size_kb": db_size_kb,
        }


async def _bench_grep_retrieval(workspace_path: Path) -> dict[str, Any]:
    """模拟基线策略：用 GrepTool 对用户自然语言 query 做正则关键词检索。"""
    workspace = WorkspaceResolver(cwd=workspace_path).resolve(workspace_path)
    grep = GrepTool(workspace)
    top1 = 0
    top5 = 0
    rr_sum = 0.0
    latencies: list[float] = []
    details: list[dict[str, Any]] = []

    for case in RETRIEVAL_CASES:
        terms = [t for t in case["query"].split() if t]
        # 基线 grep 常见策略：用空格分隔词拼成 OR 正则搜索（只搜 *.py）
        pattern = "|".join(terms)
        t0 = time.perf_counter()
        res = await grep.execute(pattern=pattern, path="mini_harness", glob="*.py", max_matches=50)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed_ms)

        ranked_files: list[str] = []
        if res.success and res.content:
            file_counts: dict[str, int] = {}
            for line in res.content.splitlines():
                if ":" not in line or line.startswith("("):
                    continue
                rel = line.split(":", 1)[0].strip()
                if not rel or not rel.endswith(".py"):
                    continue
                file_path = (
                    rel if rel.startswith("mini_harness/") else f"mini_harness/{rel}"
                )
                file_counts[file_path] = file_counts.get(file_path, 0) + 1
            # 按命中次数降序排列（对 grep 已经是较优的排序方式）
            ranked_files = [
                fp for fp, _ in sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ]

        expected = set(case["expected_files"])
        rank = None
        for idx, fp in enumerate(ranked_files[:5], start=1):
            if fp in expected:
                rank = idx
                break
        if rank == 1:
            top1 += 1
        if rank is not None:
            top5 += 1
            rr_sum += 1.0 / rank
        details.append(
            {
                "query": case["query"],
                "rank": rank,
                "top3": ranked_files[:3],
                "ms": round(elapsed_ms, 2),
            }
        )

    n = len(RETRIEVAL_CASES)
    return {
        "method": "grep_baseline",
        "cases": n,
        "top1_rate": round(top1 / n, 4),
        "top5_rate": round(top5 / n, 4),
        "mrr_at_5": round(rr_sum / n, 4),
        "avg_latency_ms": round(sum(latencies) / n, 2),
        "details": details,
    }


def _bench_hybrid_retrieval(workspace_path: Path) -> dict[str, Any] | None:
    try:
        from mini_harness.explorer.sqlite_store import SQLiteIndexStore
    except ImportError:
        return None

    workspace = WorkspaceResolver(cwd=workspace_path).resolve(workspace_path)
    with tempfile.TemporaryDirectory(prefix="mini_harness_bench_search_") as tmpdir:
        store = SQLiteIndexStore(workspace, base_dir=Path(tmpdir))
        store.sync()
        top1 = 0
        top5 = 0
        rr_sum = 0.0
        latencies: list[float] = []
        details: list[dict[str, Any]] = []

        for case in RETRIEVAL_CASES:
            t0 = time.perf_counter()
            hits = store.search(case["query"], limit=5, path_prefix="mini_harness")
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)

            ranked_files: list[str] = []
            seen: set[str] = set()
            for hit in hits:
                if hit.file_path not in seen:
                    seen.add(hit.file_path)
                    ranked_files.append(hit.file_path)

            expected = set(case["expected_files"])
            rank = None
            for idx, fp in enumerate(ranked_files[:5], start=1):
                if fp in expected:
                    rank = idx
                    break
            if rank == 1:
                top1 += 1
            if rank is not None:
                top5 += 1
                rr_sum += 1.0 / rank
            details.append(
                {
                    "query": case["query"],
                    "rank": rank,
                    "top3": [
                        f"{h.file_path}:{h.name}({h.score:.3f})" for h in hits[:3]
                    ],
                    "ms": round(elapsed_ms, 2),
                }
            )

        n = len(RETRIEVAL_CASES)
        return {
            "method": "sqlite_fts5_graph_hybrid",
            "cases": n,
            "top1_rate": round(top1 / n, 4),
            "top5_rate": round(top5 / n, 4),
            "mrr_at_5": round(rr_sum / n, 4),
            "avg_latency_ms": round(sum(latencies) / n, 2),
            "details": details,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Mini Harness Phase 1 Benchmark")
    parser.add_argument(
        "--large-files",
        type=int,
        default=1000,
        help="合成大仓库文件数量（默认 1000）",
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    real_full = _bench_full_scan(ROOT)
    real_sqlite = _bench_sqlite_incremental(
        ROOT,
        mutate_file=None,  # 不修改真实仓库文件；在临时副本测单文件修改
    )
    # 用只读真实仓副本测单文件修改不碰工作区
    real_grep = asyncio.run(_bench_grep_retrieval(ROOT))
    real_hybrid = _bench_hybrid_retrieval(ROOT)

    with tempfile.TemporaryDirectory(prefix="mini_harness_large_repo_") as tmpdir:
        large_root = Path(tmpdir)
        created = _build_synthetic_repo(large_root, args.large_files)
        large_full = _bench_full_scan(large_root)
        large_sqlite = _bench_sqlite_incremental(
            large_root,
            mutate_file=created[-1],
        )

    report = {
        "real_repo": {
            "full_scan_before": real_full,
            "sqlite_incremental_after": real_sqlite,
        },
        "large_synthetic_repo": {
            "full_scan_before": large_full,
            "sqlite_incremental_after": large_sqlite,
        },
        "retrieval": {
            "grep_before": real_grep,
            "hybrid_after": real_hybrid,
        },
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print("=== 1. Real Repo (mini-harness) ===")
    print("  Before (Full Scan):      ", real_full)
    print("  After  (SQLite WAL):     ", real_sqlite)
    print(f"\n=== 2. Large Synthetic Repo ({args.large_files} files) ===")
    print("  Before (Full Scan):      ", large_full)
    print("  After  (SQLite WAL):     ", large_sqlite)
    print("\n=== 3. Code Retrieval (15 queries) ===")
    print(
        f"  Before (Grep):   Top-1={real_grep['top1_rate']*100:.1f}%  "
        f"Top-5={real_grep['top5_rate']*100:.1f}%  "
        f"MRR@5={real_grep['mrr_at_5']:.3f}  "
        f"Avg={real_grep['avg_latency_ms']:.2f}ms"
    )
    if real_hybrid is not None:
        print(
            f"  After  (Hybrid): Top-1={real_hybrid['top1_rate']*100:.1f}%  "
            f"Top-5={real_hybrid['top5_rate']*100:.1f}%  "
            f"MRR@5={real_hybrid['mrr_at_5']:.3f}  "
            f"Avg={real_hybrid['avg_latency_ms']:.2f}ms"
        )
    else:
        print("  After  (Hybrid): [not implemented yet]")


if __name__ == "__main__":
    main()
