"""手动运行 Code Explorer 扫描与 CodeGraph 的轻量入口。

用法::

    python -m mini_harness.explorer --workspace /path/to/project
    python -m mini_harness.explorer --workspace . --save
    python -m mini_harness.explorer --workspace . --json
"""

from __future__ import annotations

import argparse
import json
import sys

from mini_harness.workspace import WorkspaceError, WorkspaceResolver

from .codegraph import build_code_graph
from .scanner import scan_project
from .store import IndexStore


def build_parser() -> argparse.ArgumentParser:
    """构造手动扫描参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m mini_harness.explorer",
        description="扫描 Python 项目并输出 Code Explorer AST 索引。",
    )
    parser.add_argument(
        "--workspace",
        metavar="PATH",
        default=None,
        help="要扫描的 Workspace；默认使用当前目录。",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="将索引保存到 ~/.mini-harness/explorer/<workspace_id>/index.json",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="将完整索引 JSON 输出到 stdout。",
    )
    parser.add_argument(
        "--graph",
        action="store_true",
        help="额外构建 CodeGraph 并输出图统计。",
    )
    parser.add_argument(
        "--graph-json",
        action="store_true",
        help="输出完整 CodeGraph JSON（隐含 --graph）。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行一次扫描并返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        workspace = WorkspaceResolver().resolve(args.workspace)
    except WorkspaceError as exc:
        parser.error(str(exc))

    index = scan_project(workspace)
    graph = (
        build_code_graph(index)
        if args.graph or args.graph_json
        else None
    )
    if args.save:
        path = IndexStore(workspace).save(index)
        print(f"索引已保存：{path}", file=sys.stderr)

    if args.graph_json:
        assert graph is not None
        print(json.dumps(graph.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.json:
        print(json.dumps(index.to_dict(), ensure_ascii=False, indent=2))
        return 0

    stats = index.stats
    print(f"Workspace: {workspace.path}")
    print(
        "索引："
        f"{stats['indexedFiles']}/{stats['files']} 个文件，"
        f"{stats['modules']} 模块 / {stats['classes']} 类 / "
        f"{stats['functions']} 函数 / {stats['methods']} 方法，"
        f"{stats['imports']} imports / {stats['calls']} calls"
    )
    if graph is not None:
        graph_stats = graph.stats
        print(
            "CodeGraph："
            f"{graph_stats['nodes']} 节点 / {graph_stats['edges']} 边，"
            f"{graph_stats['contains']} contains / "
            f"{graph_stats['imports']} imports / "
            f"{graph_stats['calls']} calls，"
            f"{graph_stats['unresolved']} unresolved"
        )
    if index.diagnostics:
        print(f"诊断：{len(index.diagnostics)} 条")
        for diagnostic in index.diagnostics[:20]:
            location = diagnostic.file_path or "<project>"
            if diagnostic.line is not None:
                location += f":{diagnostic.line}"
            print(
                f"  [{diagnostic.severity}] {diagnostic.code} "
                f"{location} - {diagnostic.message}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
