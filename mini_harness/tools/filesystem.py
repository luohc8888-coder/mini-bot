"""Workspace 文件读写工具。

实现参考 HKUDS/nanobot 的 ``list_dir`` / ``grep`` / ``read_file`` / ``write_file`` 工具：
- 列出目录内容，方便 Agent 发现项目文件；
- 用 grep 搜索关键字，避免无目的读取所有文件；
- 读取文本文件时返回带行号的内容，支持 offset/limit 分页；
- 写入时创建父目录并覆盖目标文件；
- 所有路径都通过 Workspace.resolve_path() 校验，不允许逃出 Workspace。

这里只实现 Markdown/文本文件需要的最小能力，不包含 Nanobot 的图片、
PDF、Office 文档读取，也不实现 edit_file 或 shell 命令工具。
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from mini_harness.workspace import Workspace, WorkspaceError

from .base import Tool, ToolResult

_MAX_READ_BYTES = 2 * 1024 * 1024
_MAX_OUTPUT_CHARS = 128_000
_DEFAULT_LINE_LIMIT = 2000


class ReadFileTool(Tool):
    """读取 Workspace 内的 UTF-8 文本文件，并返回带行号的内容。"""

    name = "read_file"
    description = (
        "读取 Workspace 内的 UTF-8 文本/Markdown 文件。"
        "返回内容带 1-based 行号，可使用 offset/limit 读取指定行范围。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于 Workspace 的文件路径，例如 docs/note.md",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号，默认 1",
                "minimum": 1,
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少行，默认 2000",
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
        offset: int = 1,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """读取文件内容。

        Args:
            path: 相对于 Workspace 的路径。
            offset: 起始行号，1-based。
            limit: 最多返回行数；为空时使用默认值 2000。
            **kwargs: 预留参数，ToolRegistry 传入的额外字段会被忽略。

        Returns:
            ToolResult；成功时 content 为带行号的文本，失败时 is_error 为
            True，content 为可读错误信息。
        """
        try:
            target = self.workspace.resolve_path(path)
            if not target.exists():
                return ToolResult.error(f"Error: File not found: {path}")
            if not target.is_file():
                return ToolResult.error(f"Error: Not a file: {path}")

            size = target.stat().st_size
            if size > _MAX_READ_BYTES:
                return ToolResult.error(
                    f"Error: File too large to read ({size} bytes). "
                    f"Maximum is {_MAX_READ_BYTES} bytes."
                )

            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                return ToolResult.error(
                    f"Error: Cannot read non-UTF-8/binary file {path}: {exc}"
                )

            # Nanobot 同样把 CRLF 统一成 LF，保证不同平台的 offset/limit
            # 和后续编辑结果一致。
            text = text.replace("\r\n", "\n")
            if not text:
                return ToolResult(content=f"(Empty file: {path})")

            lines = text.splitlines()
            total = len(lines)

            if offset < 1:
                offset = 1
            if offset > total:
                return ToolResult.error(
                    f"Error: offset {offset} is beyond end of file ({total} lines)"
                )

            if limit is None:
                limit = _DEFAULT_LINE_LIMIT
            if limit < 1:
                return ToolResult.error("Error: limit must be >= 1")

            start = offset - 1
            end = min(start + limit, total)
            numbered = [
                f"{start + index + 1}| {line}"
                for index, line in enumerate(lines[start:end])
            ]
            output = "\n".join(numbered)

            if len(output) > _MAX_OUTPUT_CHARS:
                clipped: list[str] = []
                used = 0
                for line in numbered:
                    extra = len(line) + (1 if clipped else 0)
                    if used + extra > _MAX_OUTPUT_CHARS:
                        break
                    clipped.append(line)
                    used += extra
                output = "\n".join(clipped)
                end = start + len(clipped)
                if not clipped:
                    output = "(File content truncated: first line exceeds output limit)"
                output += (
                    "\n\n(Output truncated at "
                    f"{_MAX_OUTPUT_CHARS} characters; showing through line {end}.)"
                )
            elif end < total:
                output += (
                    f"\n\n(Showing lines {offset}-{end} of {total}. "
                    f"Use offset={end + 1} to continue.)"
                )
            else:
                output += f"\n\n(End of file — {total} lines total)"

            return ToolResult(content=output)
        except WorkspaceError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error reading file: {exc}")


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

            if recursive:
                candidates = sorted(target.rglob("*"))
            else:
                candidates = sorted(target.iterdir())

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
            target = self.workspace.resolve_path(path)
            if not target.exists():
                return ToolResult.error(f"Error: Path not found: {path}")
            if target.is_file():
                candidates = [target]
                base = target.parent
            elif target.is_dir():
                candidates = sorted(target.rglob(glob))
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
                    if candidate.stat().st_size > _MAX_READ_BYTES:
                        continue
                    text = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
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


class WriteFileTool(Tool):
    """在 Workspace 内创建或覆盖 UTF-8 文本文件。"""

    name = "write_file"
    description = (
        "在 Workspace 内创建或覆盖 UTF-8 文本/Markdown 文件。"
        "会自动创建父目录，写入内容会完整替换目标文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于 Workspace 的文件路径，例如 docs/note.md",
            },
            "content": {
                "type": "string",
                "description": "要写入的完整文本内容",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        """保存用于解析路径的 Workspace。"""
        self.workspace = workspace

    async def execute(self, path: str, content: str, **kwargs: Any) -> ToolResult:
        """创建或覆盖文件。

        Args:
            path: 相对于 Workspace 的路径。
            content: 要写入的完整文本内容。
            **kwargs: 预留参数，ToolRegistry 传入的额外字段会被忽略。

        Returns:
            ToolResult；成功时 content 为写入摘要。
        """
        if not isinstance(content, str):
            return ToolResult.error("Error: content must be a string")

        try:
            target = self.workspace.resolve_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(target, content)
        except WorkspaceError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error writing file: {exc}")

        return ToolResult(
            content=f"Successfully wrote {len(content)} characters to {target}"
        )


def _atomic_write_text(target: Path, content: str) -> None:
    """用同目录临时文件 + ``os.replace`` 原子写入文本。

    Nanobot 的 write_file 直接 ``Path.write_text``；这里保留相同的工具语义，
    但增加原子替换，避免进程异常时留下半个文件。已有文件会保留原权限位。
    """
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        pass

    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
