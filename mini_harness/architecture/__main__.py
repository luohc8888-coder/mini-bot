"""Phase 4.2/4.3 手动入口：构建分析输入，或调用 LLM 生成运行架构。

用法::

    python -m mini_harness.architecture --workspace /path/to/project
    python -m mini_harness.architecture --workspace . --json
    python -m mini_harness.architecture --workspace . --analyze
    python -m mini_harness.architecture --workspace . --analyze --profile Qwen-token --force
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mini_harness.explorer import build_code_graph, scan_project
from mini_harness.workspace import WorkspaceError, WorkspaceResolver

from .input import ArchitectureInputBuilder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mini_harness.architecture",
        description="构建 ArchitectureGraph 分析输入，或调用 LLM 生成运行架构（Phase 4.2/4.3）。",
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
        help="输出完整 JSON（--analyze 时输出分析结果，否则输出输入包）。",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="调用 LLM 生成并校验 ArchitectureGraph。",
    )
    parser.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help="--analyze 使用的 Provider profile；省略时使用默认配置。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="--analyze 时忽略已有缓存。",
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

    if args.analyze:
        from mini_harness.web.provider import load_architecture_provider

        from .analyzer import ArchitectureAnalyzer
        from .cache import ArchitectureCache
        from .service import ArchitectureService

        selection = load_architecture_provider(args.profile)
        if selection.provider is None:
            print(f"架构分析不可用：{selection.error}", file=sys.stderr)
            return 2
        service = ArchitectureService(
            index,
            graph,
            analyzer=ArchitectureAnalyzer(selection.provider),
            cache=ArchitectureCache.for_workspace(graph.workspace_id),
            profile_name=selection.profile_name,
        )
        try:
            result = asyncio.run(service.generate(force=args.force))
        except Exception as exc:
            print(f"架构分析失败：{exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0
        print(f"Provider: {result.provider} / {result.model}")
        print(f"缓存命中: {result.cached}")
        print(f"视图: {', '.join(result.graph.views_by_id)}")
        print(
            "Token: "
            f"prompt={result.prompt_tokens} completion={result.completion_tokens}"
        )
        print(f"源码引用警告: {len(result.warnings)}")
        return 0

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
