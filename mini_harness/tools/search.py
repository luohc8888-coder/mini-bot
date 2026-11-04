"""Workspace 内的目录发现、文件名匹配与文本搜索工具。"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from mini_harness.workspace import Workspace, WorkspaceError

from ._file_io import _MAX_READ_BYTES
from .base import Tool, ToolResult

_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _validate_glob(pattern: str) -> tuple[str, ...]:
    """只接受相对 glob；禁止通过模式中的父目录组件越过搜索起点。"""
    parts = Path(pattern).parts
    if not parts or Path(pattern).is_absolute() or ".." in parts:
        raise ValueError("pattern must be a relative glob without '..'")
    if any("**" in part and part != "**" for part in parts):
        raise ValueError("'**' must be an entire path component")
    return parts


def _matches_glob(relative: Path, pattern: tuple[str, ...]) -> bool:
    """匹配 rglob 的隐含 **/ 前缀；** 可以跨零个或多个目录。"""
    parts = relative.parts
    pattern = ("**", *pattern)

    @lru_cache(maxsize=None)
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern):
            return path_index == len(parts)
        part = pattern[pattern_index]
        if part == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(parts)
            and fnmatch.fnmatchcase(parts[path_index], part)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _iter_paths(
    workspace: Workspace, root: Path, *, recursive: bool = True
) -> Iterator[Path]:
    """按稳定顺序遍历，在进入噪声目录或工作区外链接之前剪枝。

    不递归符号链接目录，避免别名重复和链接环；工作区内文件链接可作为结果。
    """
    stack = [iter(sorted(root.iterdir()))]
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if item.name in _IGNORE_DIRS:
            continue
        try:
            workspace.resolve_path(item)
        except (WorkspaceError, OSError, RuntimeError):
            continue
        yield item
        if recursive and item.is_dir() and not item.is_symlink():
            try:
                stack.append(iter(sorted(item.iterdir())))
            except PermissionError:
                continue


class ListDirTool(Tool):
    """列出 Workspace 内的目录内容，供 Agent 发现要分析的文件。"""

    name = "list_dir"
    description = (
        "列出 Workspace 内的目录内容。"
        "默认只列一层，recursive=true 时递归列出所有文件；"
        "会自动忽略 .git、.venv、__pycache__、node_modules 等噪声目录。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于 Workspace 的目录路径，例如 . 或 docs",
            },
            "recursive": {
                "type": "boolean",
                "description": "是否递归列出子目录，默认 false",
            },
            "max_entries": {
                "type": "integer",
                "description": "最多返回多少项，默认 200",
                "minimum": 1,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        """保存用于解析路径的 Workspace。"""
        self.workspace = workspace

    async def execute(
        self,
        path: str,
        recursive: bool = False,
        max_entries: int = 200,
        **kwargs: Any,
    ) -> ToolResult:
        """列出目录内容。

        Args:
            path: 相对于 Workspace 的目录路径。
            recursive: 是否递归列出所有后代文件。
            max_entries: 最多返回的条目数。
            **kwargs: 预留参数。

        Returns:
            ToolResult；成功时 content 为逐行目录列表。
        """
        if max_entries < 1:
            return ToolResult.error("Error: max_entries must be >= 1")

        try:
            target = self.workspace.resolve_path(path)
            if not target.exists():
                return ToolResult.error(f"Error: Directory not found: {path}")
            if not target.is_dir():
                return ToolResult.error(f"Error: Not a directory: {path}")

            candidates = _iter_paths(self.workspace, target, recursive=recursive)

            entries: list[str] = []
            total = 0
            for item in candidates:
                relative = item.relative_to(target)
                if any(part in _IGNORE_DIRS for part in relative.parts):
                    continue
                total += 1
                if len(entries) >= max_entries:
                    continue
                suffix = "/" if item.is_dir() else ""
                entries.append(f"{relative}{suffix}")

            if not entries:
                return ToolResult(content=f"Directory {path} is empty")

            output = "\n".join(entries)
            if total > max_entries:
                output += (
                    f"\n\n(truncated, showing first {max_entries} of "
                    f"{total} entries)"
                )
            return ToolResult(content=output)
        except WorkspaceError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error listing directory: {exc}")


class FindFilesTool(Tool):
    """按 glob 模式查找 Workspace 内的文件或目录。"""

    name = "find_files"
    description = (
        "按 glob 模式查找 Workspace 内的文件或目录。"
        "支持 kind=file/dir/any，默认只找文件；"
        "会自动忽略 .git、.venv、__pycache__、node_modules 等噪声目录。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "文件名或相对路径 glob，例如 *.py 或 **/*.py",
            },
            "path": {
                "type": "string",
                "description": "相对于 Workspace 的搜索起点目录，默认 .",
                "default": ".",
            },
            "kind": {
                "type": "string",
                "enum": ["file", "dir", "any"],
                "description": "要查找的条目类型，默认 file",
                "default": "file",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回多少项，默认 200",
                "minimum": 1,
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        """保存用于解析路径的 Workspace。"""
        self.workspace = workspace

    async def execute(
        self,
        pattern: str = "",
        path: str = ".",
        kind: str = "file",
        max_results: int = 200,
        **kwargs: Any,
    ) -> ToolResult:
        """按 glob 模式查找条目。

        Args:
            pattern: 相对 glob，例如 ``*.py`` 或 ``**/*.py``。
            path: 搜索起点目录，相对于 Workspace。
            kind: ``file``、``dir`` 或 ``any``。
            max_results: 最多返回的条目数。
            **kwargs: 预留参数，ToolRegistry 传入的额外字段会被忽略。

        Returns:
            ToolResult；成功时 content 为逐行相对路径，目录带 ``/`` 后缀。
        """
        if not pattern:
            return ToolResult.error("Error: pattern must not be empty")
        if kind not in {"file", "dir", "any"}:
            return ToolResult.error("Error: kind must be file, dir or any")
        if max_results < 1:
            return ToolResult.error("Error: max_results must be >= 1")
        try:
            pattern_parts = _validate_glob(pattern)
        except ValueError as exc:
            return ToolResult.error(f"Error: invalid glob pattern: {exc}")

        try:
            target = self.workspace.resolve_path(path)
            if not target.exists():
                return ToolResult.error(f"Error: Directory not found: {path}")
            if not target.is_dir():
                return ToolResult.error(f"Error: Not a directory: {path}")

            candidates = _iter_paths(self.workspace, target)

            entries: list[str] = []
            total = 0
            for item in candidates:
                relative = item.relative_to(target)
                if not _matches_glob(relative, pattern_parts):
                    continue
                if pattern.endswith(os.sep) and not item.is_dir():
                    continue
                if any(part in _IGNORE_DIRS for part in relative.parts):
                    continue

                is_directory = item.is_dir()
                if kind == "file" and not item.is_file():
                    continue
                if kind == "dir" and not is_directory:
                    continue
                if kind == "any" and not (is_directory or item.is_file()):
                    continue

                total += 1
                if len(entries) >= max_results:
                    continue
                suffix = "/" if is_directory else ""
                entries.append(f"{relative}{suffix}")

            if not entries:
                return ToolResult(content=f"No matches found for {pattern!r}")
            output = "\n".join(entries)
            if total > max_results:
                output += (
                    f"\n\n(truncated, showing first {max_results} of "
                    f"{total} entries)"
                )
            return ToolResult(content=output)
        except WorkspaceError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error finding files: {exc}")


class GrepTool(Tool):
    """在 Workspace 的 UTF-8 文本中搜索关键字或正则表达式。"""

    name = "grep"
    description = (
        "在 Workspace 内的文本文件中搜索正则表达式。"
        "返回文件路径、行号和命中行；适合定位类、函数或关键字后再读取文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "要搜索的正则表达式，例如 class AgentRunner",
            },
            "path": {
                "type": "string",
                "description": "相对于 Workspace 的文件或目录路径，默认 .",
                "default": ".",
            },
            "glob": {
                "type": "string",
                "description": "可选的 glob 文件过滤，例如 *.py 或 **/*.py",
                "default": "*",
            },
            "max_matches": {
                "type": "integer",
                "description": "最多返回多少条命中，默认 100",
                "minimum": 1,
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        """保存用于解析路径的 Workspace。"""
        self.workspace = workspace

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str = "*",
        max_matches: int = 100,
        **kwargs: Any,
    ) -> ToolResult:
        """执行文本搜索。

        Args:
            pattern: 正则表达式。
            path: 搜索起点，相对于 Workspace。
            glob: 文件过滤模式。
            max_matches: 最多返回的命中条数。
            **kwargs: 预留参数。

        Returns:
            ToolResult；成功时 content 为 ``path:line: text`` 形式的命中行。
        """
        if max_matches < 1:
            return ToolResult.error("Error: max_matches must be >= 1")
        try:
            expression = re.compile(pattern)
        except re.error as exc:
            return ToolResult.error(f"Error: invalid regex: {exc}")
        try:
            pattern_parts = _validate_glob(glob)
        except ValueError as exc:
            return ToolResult.error(f"Error: invalid glob pattern: {exc}")

        try:
            target = self.workspace.resolve_path(path)
            if not target.exists():
                return ToolResult.error(f"Error: Path not found: {path}")
            if target.is_file():
                candidates = [target]
                base = target.parent
            elif target.is_dir():
                candidates = (
                    item for item in _iter_paths(self.workspace, target)
                    if _matches_glob(item.relative_to(target), pattern_parts)
                )
                base = target
            else:
                return ToolResult.error(f"Error: Unsupported path: {path}")

            matches: list[str] = []
            total = 0
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(base)
                if any(part in _IGNORE_DIRS for part in relative.parts):
                    continue
                try:
                    candidate = self.workspace.resolve_path(candidate)
                    if candidate.stat().st_size > _MAX_READ_BYTES:
                        continue
                    text = candidate.read_text(encoding="utf-8")
                except (WorkspaceError, OSError, UnicodeDecodeError):
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if not expression.search(line):
                        continue
                    total += 1
                    if len(matches) < max_matches:
                        matches.append(
                            f"{relative}:{line_number}: {line.strip()}"
                        )

            if not matches:
                return ToolResult(content=f"No matches found for {pattern!r}")
            output = "\n".join(matches)
            if total > max_matches:
                output += (
                    f"\n\n(truncated, showing first {max_matches} of "
                    f"{total} matches)"
                )
            return ToolResult(content=output)
        except WorkspaceError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error searching files: {exc}")
