"""Phase 4.2 手动入口：从 CodeGraph 构建并打印语义架构分析输入。

用法::

    python -m mini_harness.architecture --workspace /path/to/project
    python -m mini_harness.architecture --workspace . --json
"""

from __future__ import annotations

import argparse
import json

from mini_harness.explorer import build_code_graph, scan_project
from mini_harness.workspace import WorkspaceError, WorkspaceResolver

from .input import ArchitectureInputBuilder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mini_harness.architecture",
        description="构建 ArchitectureGraph 语义分析输入（Phase 4.2）。",
    )
    parser.add_argument(
        "--workspace",
        metavar="PATH",
        default=None,
        help="要分析的 Workspace；默认使用当前目录。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出完整 ArchitectureAnalysisInput JSON。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        workspace = WorkspaceResolver().resolve(args.workspace)
    except WorkspaceError as exc:
        parser.error(str(exc))

    index = scan_project(workspace)
    graph = build_code_graph(index)
    analysis_input = ArchitectureInputBuilder(index, graph).build()

    if args.json:
        print(analysis_input.to_json(indent=2))
        return 0

    print(f"Workspace: {analysis_input.workspace_root}")
    print(
        "输入规模："
        f"{len(analysis_input.entry_points)} 个入口候选 / "
        f"{len(analysis_input.modules)} 个模块 / "
        f"{len(analysis_input.key_symbols)} 个关键符号 / "
        f"{len(analysis_input.imports)} 条 import / "
        f"{len(analysis_input.calls)} 条 call / "
        f"{len(analysis_input.source_excerpts)} 段源码"
    )
    print("入口候选：")
    for entry in analysis_input.entry_points[:8]:
        reasons = "；".join(entry.reasons)
        print(f"  - {entry.symbol.qualified_name}（{entry.score}）：{reasons}")
    print("关键组件候选：")
    for symbol in analysis_input.key_symbols[:10]:
        print(f"  - {symbol.qualified_name} [{symbol.file_path}:{symbol.start_line}]")
    print(
        "源码窗口字符数："
        f"{sum(len(item.text) for item in analysis_input.source_excerpts)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
