"""Phase 5：只读 Git 状态与 Diff 查询服务（严格白名单子命令，防命令与路径注入）。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_harness.explorer import CodeGraph, SymbolRecord
from mini_harness.workspace import Workspace, WorkspacePathError

_GIT_TIMEOUT_SECONDS = 3.0
_MAX_DIFF_LINES = 800
_MAX_UNTRACKED_BYTES = 200_000
_MAX_STATUS_FILES = 200


class GitServiceError(ValueError):
    """只读 Git 查询参数非法或路径越界错误。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


def _run_git(workspace_path: Path, args: list[str]) -> _GitResult | None:
    """执行只读 git 子命令（固定 shell=False，超时保护）。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(workspace_path),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _GitResult(proc.returncode, proc.stdout, proc.stderr)


def _index_symbols_by_file(graph: CodeGraph) -> dict[str, list[SymbolRecord]]:
    by_file: dict[str, list[SymbolRecord]] = {}
    for node in graph.nodes.values():
        by_file.setdefault(node.file_path, []).append(node)
    for items in by_file.values():
        items.sort(
            key=lambda s: (
                0 if s.kind == "module" else 1,
                s.start_line,
                s.qualified_name,
            )
        )
    return by_file


def _primary_symbol_for_file(
    by_file: dict[str, list[SymbolRecord]],
    rel_path: str,
) -> SymbolRecord | None:
    items = by_file.get(rel_path)
    if not items:
        return None
    return items[0]


def _classify_porcelain_status(xy: str) -> tuple[str, str]:
    """将 porcelain 两字符状态码归类为 (code, category)。"""
    if xy == "??":
        return "?", "untracked"
    x = xy[0] if len(xy) > 0 else " "
    y = xy[1] if len(xy) > 1 else " "
    if "D" in (x, y):
        return "D", "deleted"
    if "A" in (x, y):
        return "A", "added"
    if "R" in (x, y):
        return "R", "renamed"
    return "M", "modified"


def _parse_porcelain_z(raw: str) -> list[tuple[str, str]]:
    """解析 `git status --porcelain=v1 -z -uall` 输出，安全处理空格与中文路径。"""
    entries: list[tuple[str, str]] = []
    tokens = raw.split("\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 4:
            continue
        xy = token[:2]
        path = token[3:]
        # Rename/copy entries in -z mode emit an extra original-path token next
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            i += 1
        if path:
            entries.append((xy, path))
    return entries


def _is_git_repo(workspace_path: Path) -> bool:
    check = _run_git(workspace_path, ["rev-parse", "--is-inside-work-tree"])
    return check is not None and check.returncode == 0 and check.stdout.strip() == "true"


def get_git_status(workspace: Workspace, graph: CodeGraph) -> dict[str, Any]:
    """返回 Workspace 的只读 Git 仓库状态与变更文件清单。"""
    if not _is_git_repo(workspace.path):
        return {
            "available": False,
            "reason": "当前 Workspace 不是 Git 仓库或系统未安装 git。",
            "branch": None,
            "head": None,
            "files": [],
            "truncated": False,
            "summary": {
                "total": 0,
                "modified": 0,
                "added": 0,
                "deleted": 0,
                "renamed": 0,
                "untracked": 0,
            },
        }

    branch_res = _run_git(workspace.path, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = (
        branch_res.stdout.strip()
        if branch_res is not None and branch_res.returncode == 0
        else "HEAD"
    )

    head_info: dict[str, str] | None = None
    log_res = _run_git(
        workspace.path,
        ["log", "-1", "--format=%h%x1f%s%x1f%cr"],
    )
    if log_res is not None and log_res.returncode == 0 and log_res.stdout.strip():
        parts = log_res.stdout.strip().split("\x1f")
        if len(parts) >= 2:
            head_info = {
                "hash": parts[0],
                "subject": parts[1],
                "relativeTime": parts[2] if len(parts) >= 3 else "",
            }

    status_res = _run_git(
        workspace.path,
        ["status", "--porcelain=v1", "-z", "-uall"],
    )
    raw_entries = (
        _parse_porcelain_z(status_res.stdout)
        if status_res is not None and status_res.returncode == 0
        else []
    )

    by_file = _index_symbols_by_file(graph)
    summary = {
        "total": 0,
        "modified": 0,
        "added": 0,
        "deleted": 0,
        "renamed": 0,
        "untracked": 0,
    }
    files: list[dict[str, Any]] = []
    for xy, rel_path in raw_entries:
        code, category = _classify_porcelain_status(xy)
        summary["total"] += 1
        summary[category] += 1
        primary = _primary_symbol_for_file(by_file, rel_path)
        files.append(
            {
                "path": rel_path,
                "status": code,
                "category": category,
                "staged": xy[0] not in (" ", "?"),
                "unstaged": xy[1] not in (" ",),
                "symbolId": primary.id if primary is not None else None,
                "symbolName": primary.name if primary is not None else None,
                "symbolCount": len(by_file.get(rel_path, [])),
            }
        )

    # 将已索引的源码变更排在前面，便于优先查看核心代码变更
    files.sort(key=lambda item: (0 if item["symbolId"] else 1, item["path"]))
    truncated = len(files) > _MAX_STATUS_FILES
    if truncated:
        files = files[:_MAX_STATUS_FILES]

    return {
        "available": True,
        "reason": None,
        "branch": branch,
        "head": head_info,
        "files": files,
        "truncated": truncated,
        "summary": summary,
    }


def _parse_unified_diff(raw_diff: str) -> tuple[list[dict[str, Any]], int, int, bool, bool]:
    """将 unified diff 解析为带新旧行号的结构化行数组。"""
    lines: list[dict[str, Any]] = []
    additions = 0
    deletions = 0
    is_binary = False
    truncated = False
    old_line: int | None = None
    new_line: int | None = None

    for raw_line in raw_diff.splitlines():
        if raw_line.startswith("Binary files ") or raw_line.startswith("GIT binary patch"):
            is_binary = True
            lines.append(
                {
                    "kind": "meta",
                    "oldLine": None,
                    "newLine": None,
                    "text": raw_line,
                }
            )
            continue

        if len(lines) >= _MAX_DIFF_LINES:
            truncated = True
            break

        if raw_line.startswith("@@"):
            # 解析 @@ -old_start,old_len +new_start,new_len @@
            old_line = None
            new_line = None
            parts = raw_line.split("@@")
            if len(parts) >= 2:
                header = parts[1].strip().split()
                for token in header:
                    if token.startswith("-"):
                        num = token[1:].split(",", 1)[0]
                        if num.isdigit():
                            old_line = int(num)
                    elif token.startswith("+"):
                        num = token[1:].split(",", 1)[0]
                        if num.isdigit():
                            new_line = int(num)
            lines.append(
                {
                    "kind": "hunk",
                    "oldLine": None,
                    "newLine": None,
                    "text": raw_line,
                }
            )
        elif raw_line.startswith(("diff --git ", "index ", "--- ", "+++ ", "new file ", "deleted file ", "rename ")):
            lines.append(
                {
                    "kind": "meta",
                    "oldLine": None,
                    "newLine": None,
                    "text": raw_line,
                }
            )
        elif raw_line.startswith("+"):
            additions += 1
            lines.append(
                {
                    "kind": "add",
                    "oldLine": None,
                    "newLine": new_line,
                    "text": raw_line[1:],
                }
            )
            if new_line is not None:
                new_line += 1
        elif raw_line.startswith("-"):
            deletions += 1
            lines.append(
                {
                    "kind": "del",
                    "oldLine": old_line,
                    "newLine": None,
                    "text": raw_line[1:],
                }
            )
            if old_line is not None:
                old_line += 1
        elif raw_line.startswith(" "):
            lines.append(
                {
                    "kind": "context",
                    "oldLine": old_line,
                    "newLine": new_line,
                    "text": raw_line[1:],
                }
            )
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1
        else:
            lines.append(
                {
                    "kind": "meta",
                    "oldLine": None,
                    "newLine": None,
                    "text": raw_line,
                }
            )

    return lines, additions, deletions, is_binary, truncated


def _synthetic_untracked_diff(target: Path, rel_path: str) -> tuple[list[dict[str, Any]], int, int, bool, bool]:
    """为未追踪的新增文件生成只读新增预览 diff。"""
    if not target.is_file():
        return [], 0, 0, False, False
    try:
        size = target.stat().st_size
    except OSError:
        return [], 0, 0, False, False
    if size > _MAX_UNTRACKED_BYTES:
        return (
            [
                {
                    "kind": "meta",
                    "oldLine": None,
                    "newLine": None,
                    "text": f"新增文件过大（{size} bytes > {_MAX_UNTRACKED_BYTES} bytes），已省略预览。",
                }
            ],
            0,
            0,
            False,
            True,
        )
    try:
        raw_bytes = target.read_bytes()
    except OSError:
        return [], 0, 0, False, False
    if b"\x00" in raw_bytes[:4096]:
        return (
            [
                {
                    "kind": "meta",
                    "oldLine": None,
                    "newLine": None,
                    "text": f"Binary untracked file: {rel_path}",
                }
            ],
            0,
            0,
            True,
            False,
        )
    text = raw_bytes.decode("utf-8", errors="replace")
    file_lines = text.splitlines()
    truncated = len(file_lines) > _MAX_DIFF_LINES
    shown = file_lines[:_MAX_DIFF_LINES]
    lines: list[dict[str, Any]] = [
        {
            "kind": "meta",
            "oldLine": None,
            "newLine": None,
            "text": f"new untracked file: {rel_path}",
        },
        {
            "kind": "hunk",
            "oldLine": None,
            "newLine": None,
            "text": f"@@ -0,0 +1,{len(shown)} @@",
        },
    ]
    for idx, line_text in enumerate(shown, start=1):
        lines.append(
            {
                "kind": "add",
                "oldLine": None,
                "newLine": idx,
                "text": line_text,
            }
        )
    return lines, len(file_lines), 0, False, truncated


def get_git_diff(
    workspace: Workspace,
    graph: CodeGraph,
    file_path: str,
) -> dict[str, Any]:
    """返回指定 Workspace 内文件的只读 Git Diff 与关联代码符号。"""
    cleaned = file_path.strip()
    if not cleaned:
        raise GitServiceError("缺少 file 参数。", status_code=400)
    if cleaned.startswith("-") or "\0" in cleaned:
        raise GitServiceError(f"非法文件路径：{cleaned}", status_code=400)

    try:
        target = workspace.resolve_path(cleaned)
    except WorkspacePathError as exc:
        raise GitServiceError(str(exc), status_code=400) from exc

    rel_path = target.relative_to(workspace.path).as_posix()
    if not rel_path or rel_path == ".":
        raise GitServiceError("不能对根目录本身请求单文件 Diff。", status_code=400)

    if not _is_git_repo(workspace.path):
        raise GitServiceError("当前 Workspace 不是 Git 仓库。", status_code=503)

    # 先检查该文件是否在 git status 中且是否为 untracked
    status_res = _run_git(
        workspace.path,
        ["status", "--porcelain=v1", "-z", "-uall", "--", rel_path],
    )
    entries = (
        _parse_porcelain_z(status_res.stdout)
        if status_res is not None and status_res.returncode == 0
        else []
    )
    status_code = "M"
    category = "modified"
    if entries:
        status_code, category = _classify_porcelain_status(entries[0][0])

    if category == "untracked":
        lines, additions, deletions, is_binary, truncated = _synthetic_untracked_diff(
            target, rel_path
        )
    else:
        # 优先对比 HEAD（同时覆盖已暂存和未暂存修改）；无 HEAD 提交时回退到 --cached / 工作区 diff
        has_head = _run_git(workspace.path, ["rev-parse", "--verify", "HEAD"])
        if has_head is not None and has_head.returncode == 0:
            diff_res = _run_git(
                workspace.path,
                ["diff", "--no-color", "HEAD", "--", rel_path],
            )
        else:
            diff_res = _run_git(
                workspace.path,
                ["diff", "--no-color", "--cached", "--", rel_path],
            )
            if diff_res is not None and not diff_res.stdout.strip():
                diff_res = _run_git(
                    workspace.path,
                    ["diff", "--no-color", "--", rel_path],
                )
        raw_diff = diff_res.stdout if diff_res is not None else ""
        lines, additions, deletions, is_binary, truncated = _parse_unified_diff(raw_diff)

    by_file = _index_symbols_by_file(graph)
    symbols = [
        {
            "symbolId": sym.id,
            "name": sym.name,
            "kind": sym.kind,
            "qualifiedName": sym.qualified_name,
            "startLine": sym.start_line,
            "endLine": sym.end_line,
        }
        for sym in by_file.get(rel_path, [])[:30]
    ]

    return {
        "filePath": rel_path,
        "status": status_code,
        "category": category,
        "additions": additions,
        "deletions": deletions,
        "isBinary": is_binary,
        "truncated": truncated,
        "lines": lines,
        "symbols": symbols,
    }
