"""项目文件发现与 AST 索引构建。

扫描器负责所有语言共用的部分：目录遍历、忽略规则、文件大小限制、
Workspace 路径安全、编码检测、hash 计算和诊断信息收集。具体语言解析
委托给 ``LanguageAdapter``。
"""

from __future__ import annotations

import hashlib
import io
import os
import tokenize
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from mini_harness.workspace import (
    Workspace,
    WorkspaceError,
    WorkspacePathError,
    WorkspaceResolver,
)

from .ast_parser import PythonAstParser
from .base import LanguageAdapter
from .models import (
    DiagnosticRecord,
    FileRecord,
    ParsedFile,
    ProjectIndex,
    SCHEMA_VERSION,
    SourceFile,
)

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024

# 与现有 search/list_dir 工具保持一致的噪声目录；额外加入常见前端/后端构建目录。
DEFAULT_IGNORE_DIRS = frozenset(
    {
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
        ".tox",
        ".nox",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
        "target",
        "out",
        ".next",
        ".nuxt",
        ".turbo",
    }
)


def _decode_source(raw: bytes) -> str:
    """按 PEP 263 检测源码编码并解码。"""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return raw.decode(encoding)
    except (LookupError, SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法解码源码：{exc}") from exc


class ProjectScanner:
    """扫描 Workspace 并生成语言无关的 ``ProjectIndex``。

    默认注册 Python AST 适配器。后续扩展 Java/C++/TypeScript 时，传入额外
    ``LanguageAdapter`` 即可；扫描器本身无需修改。
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        adapters: Sequence[LanguageAdapter] | None = None,
        ignore_dirs: Iterable[str] | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须大于 0")
        self.workspace = workspace
        self.adapters = list(adapters) if adapters is not None else [PythonAstParser()]
        if not self.adapters:
            raise ValueError("至少需要注册一个 LanguageAdapter")
        self.ignore_dirs = set(
            DEFAULT_IGNORE_DIRS if ignore_dirs is None else ignore_dirs
        )
        self.max_file_bytes = max_file_bytes

    def adapter_for(self, path: Path) -> LanguageAdapter | None:
        """返回匹配文件后缀的语言适配器。"""
        for adapter in self.adapters:
            if adapter.matches(path):
                return adapter
        return None

    def iter_source_files(self) -> Iterator[Path]:
        """按稳定顺序遍历候选源文件。

        不跟随符号链接目录；符号链接文件由 ``_scan_file`` 单独检查和拒绝，
        避免同一个文件通过多个别名被重复索引。
        """
        for dirpath, dirnames, filenames in os.walk(
            self.workspace.path,
            topdown=True,
            followlinks=False,
        ):
            root = Path(dirpath)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if name not in self.ignore_dirs
                and not (root / name).is_symlink()
            ]
            for filename in sorted(filenames):
                path = root / filename
                if self.adapter_for(path) is not None:
                    yield path

    def scan(self) -> ProjectIndex:
        """扫描整个 Workspace 并返回索引。"""
        index = ProjectIndex(
            schema_version=SCHEMA_VERSION,
            workspace_root=str(self.workspace.path),
            workspace_id=self.workspace.id,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        for path in self.iter_source_files():
            relative_path = path.relative_to(self.workspace.path).as_posix()
            adapter = self.adapter_for(path)
            if adapter is None:  # pragma: no cover - iter_source_files 已保证
                continue
            file_record, parsed = self._scan_file(
                path,
                relative_path,
                adapter,
            )
            index.files.append(file_record)
            if parsed is not None:
                index.symbols.extend(parsed.symbols)
                index.imports.extend(parsed.imports)
                index.calls.extend(parsed.calls)
                index.diagnostics.extend(parsed.diagnostics)
                if any(
                    diagnostic.severity == "error"
                    for diagnostic in parsed.diagnostics
                ):
                    index.files[-1] = replace(
                        file_record,
                        parse_status="error",
                        error=_first_error(parsed.diagnostics),
                    )
        return index

    def _scan_file(
        self,
        path: Path,
        relative_path: str,
        adapter: LanguageAdapter,
    ) -> tuple[FileRecord, ParsedFile | None]:
        """读取并解析一个候选文件，所有错误都转换为诊断。"""
        if path.is_symlink():
            try:
                self.workspace.resolve_path(path)
            except (WorkspacePathError, OSError, RuntimeError) as exc:
                return self._skipped_file(
                    relative_path,
                    adapter.language,
                    "symlink_escape",
                    f"跳过指向 Workspace 外的符号链接：{exc}",
                    severity="warning",
                )
            return self._skipped_file(
                relative_path,
                adapter.language,
                "symlink_skipped",
                "跳过符号链接文件，避免重复索引。",
                severity="warning",
            )

        try:
            self.workspace.resolve_path(path)
            stat = path.stat()
        except WorkspaceError as exc:
            return self._skipped_file(
                relative_path,
                adapter.language,
                "path_outside_workspace",
                f"跳过 Workspace 外路径：{exc}",
            )
        except OSError as exc:
            return self._error_file(
                relative_path,
                adapter.language,
                "read_error",
                f"无法读取文件属性：{exc}",
            )

        if stat.st_size > self.max_file_bytes:
            return self._skipped_file(
                relative_path,
                adapter.language,
                "file_too_large",
                (
                    f"跳过超过大小限制的文件：{stat.st_size} bytes "
                    f"> {self.max_file_bytes} bytes"
                ),
                size=stat.st_size,
                severity="warning",
            )

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return self._error_file(
                relative_path,
                adapter.language,
                "read_error",
                f"无法读取文件：{exc}",
                size=stat.st_size,
            )

        if len(raw) > self.max_file_bytes:
            return self._skipped_file(
                relative_path,
                adapter.language,
                "file_too_large",
                "文件在读取期间超过大小限制。",
                size=len(raw),
                severity="warning",
            )

        try:
            text = _decode_source(raw)
        except ValueError as exc:
            return self._error_file(
                relative_path,
                adapter.language,
                "decode_error",
                str(exc),
                size=len(raw),
            )

        sha256 = hashlib.sha256(raw).hexdigest()
        line_count = len(text.splitlines())
        source = SourceFile(
            path=str(path),
            relative_path=relative_path,
            size=len(raw),
            sha256=sha256,
            text=text,
            line_count=line_count,
        )
        try:
            parsed = adapter.parse(source)
        except Exception as exc:  # noqa: BLE001 - 单文件解析不能拖垮全项目
            return self._error_file(
                relative_path,
                adapter.language,
                "adapter_error",
                f"语言适配器解析失败：{exc}",
                size=len(raw),
                sha256=sha256,
                line_count=line_count,
            )

        status = (
            "error"
            if any(item.severity == "error" for item in parsed.diagnostics)
            else "ok"
        )
        file_record = FileRecord(
            path=relative_path,
            language=adapter.language,
            size=len(raw),
            sha256=sha256,
            line_count=line_count,
            parse_status=status,
            error=_first_error(parsed.diagnostics) if status == "error" else None,
        )
        return file_record, parsed

    def _skipped_file(
        self,
        relative_path: str,
        language: str,
        code: str,
        message: str,
        *,
        size: int = 0,
        severity: str = "warning",
    ) -> tuple[FileRecord, ParsedFile | None]:
        """生成 skipped 文件记录和对应诊断。"""
        record = FileRecord(
            path=relative_path,
            language=language,
            size=size,
            sha256="",
            line_count=0,
            parse_status="skipped",
            error=message,
        )
        diagnostic = DiagnosticRecord(
            code=code,
            message=message,
            severity=severity,
            file_path=relative_path,
        )
        return record, ParsedFile(diagnostics=[diagnostic])

    def _error_file(
        self,
        relative_path: str,
        language: str,
        code: str,
        message: str,
        *,
        size: int = 0,
        sha256: str = "",
        line_count: int = 0,
    ) -> tuple[FileRecord, ParsedFile | None]:
        """生成 error 文件记录和对应诊断。"""
        record = FileRecord(
            path=relative_path,
            language=language,
            size=size,
            sha256=sha256,
            line_count=line_count,
            parse_status="error",
            error=message,
        )
        diagnostic = DiagnosticRecord(
            code=code,
            message=message,
            severity="error",
            file_path=relative_path,
        )
        return record, ParsedFile(diagnostics=[diagnostic])


def _first_error(diagnostics: Sequence[DiagnosticRecord]) -> str | None:
    """返回首个错误消息，供 FileRecord.error 使用。"""
    for diagnostic in diagnostics:
        if diagnostic.severity == "error":
            return diagnostic.message
    return None


def scan_project(
    workspace: Workspace | str | Path,
    *,
    adapters: Sequence[LanguageAdapter] | None = None,
    ignore_dirs: Iterable[str] | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> ProjectIndex:
    """扫描一个已固定 Workspace 或路径，返回项目索引。"""
    if not isinstance(workspace, Workspace):
        workspace = WorkspaceResolver().resolve(workspace)
    scanner = ProjectScanner(
        workspace,
        adapters=adapters,
        ignore_dirs=ignore_dirs,
        max_file_bytes=max_file_bytes,
    )
    return scanner.scan()


__all__ = [
    "DEFAULT_IGNORE_DIRS",
    "DEFAULT_MAX_FILE_BYTES",
    "ProjectScanner",
    "scan_project",
]
