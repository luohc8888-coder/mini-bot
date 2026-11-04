"""Workspace 内的文本读取、覆盖/追加写入与文件元信息。

目录发现与搜索在 search.py，精确文本编辑在 edit.py，命令在 command.py。
"""

from __future__ import annotations

import codecs
import json
import stat
from datetime import datetime, timezone
from typing import Any

from mini_harness.workspace import Workspace, WorkspaceError

from ._file_io import _MAX_READ_BYTES, _atomic_write_text
from .base import Tool, ToolResult

_MAX_OUTPUT_CHARS = 128_000
_DEFAULT_LINE_LIMIT = 200


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
                "description": "最多返回多少行，默认 200",
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
            limit: 最多返回行数；为空时使用默认值 200。
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
                    f"Maximum is {_MAX_READ_BYTES} bytes. "
                    "Use file_info to inspect metadata, or run_command for a targeted read."
                )

            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                return ToolResult.error(
                    f"Error: Cannot read non-UTF-8/binary file {path}: {exc}. "
                    "Use file_info to inspect metadata instead of retrying read_file."
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


class FileInfoTool(Tool):
    """有界读取文件元信息；超大文件只检测前缀，不虚报完整行数。"""

    name = "file_info"
    description = "查看 Workspace 内文件/目录的类型、字节大小、修改时间、UTF-8/二进制检测和文本行数。大文件只采样，行数返回 null。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, path: str) -> ToolResult:
        try:
            target = self.workspace.resolve_path(path)
            info = target.stat()
            kind = "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "other"
            data: dict[str, Any] = {
                "path": str(target.relative_to(self.workspace.path)),
                "type": kind,
                "size_bytes": info.st_size,
                "mtime": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                "is_binary": None,
                "line_count": None,
                "sampled": False,
            }
            if kind == "file":
                with target.open("rb") as handle:
                    sample = handle.read(_MAX_READ_BYTES + 1)
                sampled = len(sample) > _MAX_READ_BYTES
                sample = sample[:_MAX_READ_BYTES]
                data["sampled"] = sampled
                try:
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    text = decoder.decode(sample, final=not sampled)
                    data["is_binary"] = b"\x00" in sample
                    if not sampled and not data["is_binary"]:
                        data["line_count"] = len(text.splitlines())
                except UnicodeDecodeError:
                    data["is_binary"] = True
                data["binary_detection"] = "UTF-8/NUL heuristic; prefix only when sampled"
            return ToolResult(content=json.dumps(data, ensure_ascii=False))
        except (WorkspaceError, OSError) as exc:
            return ToolResult.error(f"Error inspecting file: {exc}")


class WriteFileTool(Tool):
    """在 Workspace 内创建、覆盖或追加 UTF-8 文本文件。"""

    name = "write_file"
    description = (
        "在 Workspace 内创建或覆盖 UTF-8 文本/Markdown 文件。"
        "会自动创建父目录，默认 overwrite 完整替换；mode=append 只追加 content，不自动补换行。"
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
                "description": "要写入的文本；append 模式只传新增内容",
            },
            "mode": {"type": "string", "enum": ["overwrite", "append"]},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        """保存用于解析路径的 Workspace。"""
        self.workspace = workspace

    async def execute(
        self, path: str, content: str, mode: str = "overwrite", **kwargs: Any
    ) -> ToolResult:
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
        if mode not in {"overwrite", "append"}:
            return ToolResult.error("Error: mode must be overwrite or append")

        try:
            target = self.workspace.resolve_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with target.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(content)
            else:
                _atomic_write_text(target, content)
        except WorkspaceError as exc:
            return ToolResult.error(f"Error: {exc}")
        except OSError as exc:
            return ToolResult.error(f"Error writing file: {exc}")

        action = "appended" if mode == "append" else "wrote"
        return ToolResult(content=f"Successfully {action} {len(content)} characters to {target}")
