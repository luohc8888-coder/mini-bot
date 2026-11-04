"""Workspace 内的单次与批量精确编辑，共用读取、匹配和提交逻辑。

apply_patch 接受 edits 数组，不解析 unified diff。全部校验后才开始写入；
写入失败尝试恢复原字节并报告回滚状态，不承诺进程崩溃时跨文件原子性。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_harness.workspace import Workspace, WorkspaceError

from ._file_io import _MAX_EDIT_BYTES, _atomic_write_text
from .base import Tool, ToolResult

_MAX_EDITS = 100


class _EditError(ValueError):
    """精确编辑的校验或提交错误。"""


@dataclass(slots=True)
class _FileBuffer:
    """保留原字节用于回滚，内存内容统一使用 LF。"""

    target: Path
    original_bytes: bytes
    original: str
    content: str
    uses_crlf: bool

    def rendered(self) -> str:
        return self.content.replace("\n", "\r\n") if self.uses_crlf else self.content


@dataclass(slots=True)
class _EditSummary:
    changed_files: int
    replacements: int


def _read_bytes(target: Path) -> bytes:
    """有界读取，避免 stat 与 read 之间的文件增长绕过大小限制。"""
    with target.open("rb") as handle:
        raw = handle.read(_MAX_EDIT_BYTES + 1)
    if len(raw) > _MAX_EDIT_BYTES:
        raise _EditError(
            f"File too large to edit: {target}. "
            f"Maximum is {_MAX_EDIT_BYTES} bytes."
        )
    return raw


def _load_buffer(target: Path, display_path: str) -> _FileBuffer:
    """读取可编辑的 UTF-8 文本，并保留回滚所需原字节。"""
    if not target.exists():
        raise _EditError(f"File not found: {display_path}")
    if not target.is_file():
        raise _EditError(f"Not a file: {display_path}")
    raw = _read_bytes(target)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _EditError(
            f"Cannot edit non-UTF-8/binary file {display_path}: {exc}"
        ) from exc
    if b"\x00" in raw:
        raise _EditError(
            f"Cannot edit non-UTF-8/binary file {display_path}: NUL byte found"
        )
    content = text.replace("\r\n", "\n")
    return _FileBuffer(target, raw, content, content, b"\r\n" in raw)


def _replace(buffer: _FileBuffer, edit: dict[str, Any], label: str) -> int:
    """先估算替换后的 UTF-8 大小，再分配结果字符串。"""
    old_text = edit.get("old_text")
    new_text = edit.get("new_text")
    replace_all = edit.get("replace_all", False)
    if not isinstance(old_text, str) or not old_text:
        raise _EditError(f"{label}.old_text must not be empty")
    if not isinstance(new_text, str):
        raise _EditError(f"{label}.new_text is required")
    if not isinstance(replace_all, bool):
        raise _EditError(f"{label}.replace_all must be a boolean")
    old_text = old_text.replace("\r\n", "\n")
    new_text = new_text.replace("\r\n", "\n")
    if old_text == new_text:
        raise _EditError(f"{label}.new_text must be different from old_text")
    count = buffer.content.count(old_text)
    if count == 0:
        raise _EditError(
            f"{label}: old_text not found in {edit['path']}. "
            "Read the exact content with read_file first."
        )
    if count > 1 and not replace_all:
        raise _EditError(
            f"{label}: old_text appears {count} times in {edit['path']}. "
            "Provide more context or set replace_all=true."
        )
    replaced = count if replace_all else 1
    size = len(buffer.content.encode("utf-8")) + replaced * (
        len(new_text.encode("utf-8")) - len(old_text.encode("utf-8"))
    )
    if buffer.uses_crlf:
        size += buffer.content.count("\n") + replaced * (
            new_text.count("\n") - old_text.count("\n")
        )
    if size > _MAX_EDIT_BYTES:
        raise _EditError(
            f"{label}: resulting file too large ({size} bytes). "
            f"Maximum is {_MAX_EDIT_BYTES} bytes."
        )
    buffer.content = buffer.content.replace(
        old_text, new_text, -1 if replace_all else 1
    )
    return replaced


def _commit(buffers: list[_FileBuffer]) -> int:
    """全部校验完成后提交；I/O 失败时尝试恢复已触及文件的原字节。"""
    changed = [b for b in buffers if b.content != b.original]
    for buffer in changed:
        if _read_bytes(buffer.target) != buffer.original_bytes:
            raise _EditError(
                f"File changed while preparing edits: {buffer.target}. "
                "No files were written."
            )
    attempted: list[_FileBuffer] = []
    try:
        for buffer in changed:
            attempted.append(buffer)
            _atomic_write_text(buffer.target, buffer.rendered())
    except OSError as exc:
        failed_path = buffer.target
        rollback_failed: list[str] = []
        for previous in reversed(attempted):
            try:
                if _read_bytes(previous.target) != previous.original_bytes:
                    _atomic_write_text(
                        previous.target, previous.original_bytes.decode("utf-8")
                    )
            except (OSError, _EditError):
                rollback_failed.append(str(previous.target))
        status = (
            "Rollback failed; files may remain changed: " + ", ".join(rollback_failed)
            if rollback_failed else "All touched files were restored to their original contents."
        )
        raise _EditError(f"Failed to write {failed_path}: {exc}. {status}") from exc
    return len(changed)


def _apply_edits(workspace: Workspace, edits: Any) -> _EditSummary:
    """顺序在内存叠加全部替换，任何校验失败均不落盘。"""
    if not isinstance(edits, list):
        raise _EditError("edits must be an array")
    if not edits:
        raise _EditError("edits must not be empty")
    if len(edits) > _MAX_EDITS:
        raise _EditError(f"edits must contain at most {_MAX_EDITS} items")
    buffers: dict[Path, _FileBuffer] = {}
    replacements = 0
    for index, edit in enumerate(edits):
        label = f"edits[{index}]"
        if not isinstance(edit, dict):
            raise _EditError(f"{label} must be an object")
        path = edit.get("path")
        if not isinstance(path, str) or not path:
            raise _EditError(f"{label}.path is required")
        target = workspace.resolve_path(path)
        if target not in buffers:
            buffers[target] = _load_buffer(target, path)
        replacements += _replace(buffers[target], edit, label)
    return _EditSummary(_commit(list(buffers.values())), replacements)


class EditFileTool(Tool):
    """对已有文本文件执行一次精确替换。"""

    name = "edit_file"
    description = (
        "在 Workspace 内的 UTF-8 文本/Markdown 文件中做精确字符串替换。"
        "修改前先用 read_file 读取目标内容；默认要求 old_text 恰好出现一次，"
        "replace_all=true 可替换所有匹配。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于 Workspace 的文件路径，例如 docs/note.md",
            },
            "old_text": {
                "type": "string",
                "description": "要查找的原文，必须是从 read_file 复制出的精确文本",
            },
            "new_text": {
                "type": "string",
                "description": "替换后的文本；可以为空字符串，表示删除匹配内容",
            },
            "replace_all": {
                "type": "boolean",
                "description": "是否替换全部匹配，默认 false（只允许唯一匹配）",
                "default": False,
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(
        self,
        path: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        replace_all: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        """编辑单个文件，使用与批量编辑一致的匹配和提交规则。

        Args:
            path: Workspace 内的现有文本文件。
            old_text: 要精确匹配的原文，不含 read_file 的行号前缀。
            new_text: 替换内容，空字符串表示删除匹配文本。
            replace_all: 是否替换所有匹配；默认只接受唯一匹配。
            **kwargs: 兼容工具接口的预留参数。

        Returns:
            成功摘要或结构化错误；不创建文件。
        """
        if not path:
            return ToolResult.error("Error: path is required")
        if old_text is None:
            return ToolResult.error("Error: old_text is required")
        if new_text is None:
            return ToolResult.error("Error: new_text is required")
        try:
            result = _apply_edits(self.workspace, [
                {
                    "path": path,
                    "old_text": old_text,
                    "new_text": new_text,
                    "replace_all": replace_all,
                }
            ])
        except (_EditError, WorkspaceError, OSError, UnicodeError) as exc:
            return ToolResult.error(f"Error editing file: {exc}")
        return ToolResult(content=(
            f"Successfully edited {path}: replaced {result.replacements} "
            "occurrence(s)."
        ))


class ApplyPatchTool(Tool):
    """一次执行同文件或跨文件的多个精确替换。"""

    name = "apply_patch"
    description = (
        "在 Workspace 内的 UTF-8 文本/Markdown 文件中按顺序应用多个精确替换。"
        "先校验全部编辑再写入；默认要求每处 old_text 唯一匹配，"
        "replace_all=true 可替换全部匹配。写入失败尝试回滚并报告状态。"
        "接口接受 edits 数组，不接收 unified diff。新建或整体重写文件请用 write_file。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "description": "按顺序应用的编辑列表；全部校验通过后才会写入文件",
                "minItems": 1,
                "maxItems": _MAX_EDITS,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "相对于 Workspace 的文件路径，例如 docs/note.md",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "要查找的原文，必须是从 read_file 复制出的精确文本",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "替换后的文本；可以为空字符串，表示删除匹配内容",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "是否替换全部匹配，默认 false（只允许唯一匹配）",
                            "default": False,
                        },
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["edits"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, edits: Any = None, **kwargs: Any) -> ToolResult:
        """先校验全部 edits；提交失败时尝试回滚。

        Args:
            edits: 按顺序叠加的 path/old_text/new_text/replace_all 对象列表。
            **kwargs: 兼容工具接口的预留参数。

        Returns:
            编辑数量摘要，或包含提交/回滚状态的错误结果。
            校验失败不会落盘；回滚失败会指出仍可能改变的路径。
        """
        try:
            result = _apply_edits(self.workspace, edits)
        except (_EditError, WorkspaceError, OSError, UnicodeError) as exc:
            return ToolResult.error(f"Error applying patch: {exc}")
        if result.changed_files == 0:
            return ToolResult(content=(
                f"Successfully applied {len(edits)} edit(s); no net file changes."
            ))
        return ToolResult(content=(
            f"Successfully applied {len(edits)} edit(s) across {result.changed_files} file(s); "
            f"replaced {result.replacements} occurrence(s)."
        ))


__all__ = ["ApplyPatchTool", "EditFileTool"]
