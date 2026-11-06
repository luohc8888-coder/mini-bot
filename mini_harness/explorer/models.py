"""Code Explorer 的稳定数据模型。

本模块只定义与语言无关的索引结构。不同语言的解析器最终都输出这些
dataclass；CodeGraph、持久化和前端不直接依赖具体语言的 AST 类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourceFile:
    """交给语言解析器的单文件输入。

    该对象只在内存中存在，不直接序列化到 index.json。
    """

    path: str
    relative_path: str
    size: int
    sha256: str
    text: str
    line_count: int


@dataclass(frozen=True, slots=True)
class FileRecord:
    """一个被扫描器发现并尝试处理的源文件。"""

    path: str
    language: str
    size: int
    sha256: str
    line_count: int
    parse_status: str = "ok"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 index.json 使用的 camelCase 结构。"""
        return {
            "path": self.path,
            "language": self.language,
            "size": self.size,
            "sha256": self.sha256,
            "lineCount": self.line_count,
            "parseStatus": self.parse_status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRecord:
        """从 index.json 数据恢复文件记录。"""
        return cls(
            path=str(data["path"]),
            language=str(data.get("language", "unknown")),
            size=int(data.get("size", 0)),
            sha256=str(data.get("sha256", "")),
            line_count=int(data.get("lineCount", 0)),
            parse_status=str(data.get("parseStatus", "ok")),
            error=data.get("error"),
        )


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """一个可定位的代码符号（模块、类、函数或方法）。"""

    id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None
    decorators: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    parent_id: str | None = None
    language: str = "python"

    def to_dict(self) -> dict[str, Any]:
        """转换为 index.json 使用的 camelCase 结构。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "qualifiedName": self.qualified_name,
            "filePath": self.file_path,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "signature": self.signature,
            "docstring": self.docstring,
            "decorators": list(self.decorators),
            "bases": list(self.bases),
            "parentId": self.parent_id,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SymbolRecord:
        """从 index.json 数据恢复符号记录。"""
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            name=str(data["name"]),
            qualified_name=str(data.get("qualifiedName", data["name"])),
            file_path=str(data.get("filePath", "")),
            start_line=int(data.get("startLine", 1)),
            end_line=int(data.get("endLine", data.get("startLine", 1))),
            signature=data.get("signature"),
            docstring=data.get("docstring"),
            decorators=list(data.get("decorators", [])),
            bases=list(data.get("bases", [])),
            parent_id=data.get("parentId"),
            language=str(data.get("language", "python")),
        )


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """一条 import 关系；Phase 3 再解析到具体模块/符号。"""

    file_path: str
    module: str
    name: str | None
    alias: str | None
    level: int
    line: int
    column: int

    def to_dict(self) -> dict[str, Any]:
        """转换为 index.json 使用的 camelCase 结构。"""
        return {
            "filePath": self.file_path,
            "module": self.module,
            "name": self.name,
            "alias": self.alias,
            "level": self.level,
            "line": self.line,
            "column": self.column,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportRecord:
        """从 index.json 数据恢复 import 记录。"""
        return cls(
            file_path=str(data.get("filePath", "")),
            module=str(data.get("module", "")),
            name=data.get("name"),
            alias=data.get("alias"),
            level=int(data.get("level", 0)),
            line=int(data.get("line", 1)),
            column=int(data.get("column", 0)),
        )


@dataclass(frozen=True, slots=True)
class CallRecord:
    """一条原始调用表达式；跨文件/跨作用域解析留给 Phase 3。"""

    caller_id: str
    callee_expression: str
    callee_name: str
    line: int
    column: int

    def to_dict(self) -> dict[str, Any]:
        """转换为 index.json 使用的 camelCase 结构。"""
        return {
            "callerId": self.caller_id,
            "calleeExpression": self.callee_expression,
            "calleeName": self.callee_name,
            "line": self.line,
            "column": self.column,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallRecord:
        """从 index.json 数据恢复调用记录。"""
        return cls(
            caller_id=str(data.get("callerId", "")),
            callee_expression=str(data.get("calleeExpression", "")),
            callee_name=str(data.get("calleeName", "")),
            line=int(data.get("line", 1)),
            column=int(data.get("column", 0)),
        )


@dataclass(frozen=True, slots=True)
class DiagnosticRecord:
    """扫描或解析过程中的可审计诊断信息。"""

    code: str
    message: str
    severity: str = "error"
    file_path: str | None = None
    line: int | None = None
    column: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 index.json 使用的 camelCase 结构。"""
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "filePath": self.file_path,
            "line": self.line,
            "column": self.column,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiagnosticRecord:
        """从 index.json 数据恢复诊断记录。"""
        return cls(
            code=str(data.get("code", "unknown")),
            message=str(data.get("message", "")),
            severity=str(data.get("severity", "error")),
            file_path=data.get("filePath"),
            line=data.get("line"),
            column=data.get("column"),
        )


@dataclass(slots=True)
class ParsedFile:
    """语言适配器解析单文件后的输出。"""

    symbols: list[SymbolRecord] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    diagnostics: list[DiagnosticRecord] = field(default_factory=list)


@dataclass(slots=True)
class ProjectIndex:
    """一次完整项目扫描的结果，也是 Phase 3 CodeGraph 的输入。"""

    schema_version: int
    workspace_root: str
    workspace_id: str
    generated_at: str
    files: list[FileRecord] = field(default_factory=list)
    symbols: list[SymbolRecord] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    diagnostics: list[DiagnosticRecord] = field(default_factory=list)

    @property
    def stats(self) -> dict[str, int]:
        """返回稳定的索引统计，便于 UI 和调试脚本直接消费。"""
        counts = {
            "files": len(self.files),
            "indexedFiles": sum(
                1 for record in self.files if record.parse_status == "ok"
            ),
            "symbols": len(self.symbols),
            "modules": 0,
            "classes": 0,
            "functions": 0,
            "methods": 0,
            "imports": len(self.imports),
            "calls": len(self.calls),
            "diagnostics": len(self.diagnostics),
        }
        for symbol in self.symbols:
            key = {
                "class": "classes",
                "function": "functions",
                "method": "methods",
                "module": "modules",
            }.get(symbol.kind)
            if key is not None:
                counts[key] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入 index.json 的 JSON 友好结构。"""
        return {
            "schemaVersion": self.schema_version,
            "workspaceRoot": self.workspace_root,
            "workspaceId": self.workspace_id,
            "generatedAt": self.generated_at,
            "stats": self.stats,
            "files": [record.to_dict() for record in self.files],
            "symbols": [record.to_dict() for record in self.symbols],
            "imports": [record.to_dict() for record in self.imports],
            "calls": [record.to_dict() for record in self.calls],
            "diagnostics": [record.to_dict() for record in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectIndex:
        """从 index.json 数据恢复完整索引。"""
        return cls(
            schema_version=int(data.get("schemaVersion", SCHEMA_VERSION)),
            workspace_root=str(data.get("workspaceRoot", "")),
            workspace_id=str(data.get("workspaceId", "")),
            generated_at=str(data.get("generatedAt", "")),
            files=[FileRecord.from_dict(item) for item in data.get("files", [])],
            symbols=[
                SymbolRecord.from_dict(item) for item in data.get("symbols", [])
            ],
            imports=[
                ImportRecord.from_dict(item) for item in data.get("imports", [])
            ],
            calls=[CallRecord.from_dict(item) for item in data.get("calls", [])],
            diagnostics=[
                DiagnosticRecord.from_dict(item)
                for item in data.get("diagnostics", [])
            ],
        )


__all__ = [
    "SCHEMA_VERSION",
    "CallRecord",
    "DiagnosticRecord",
    "FileRecord",
    "ImportRecord",
    "ParsedFile",
    "ProjectIndex",
    "SourceFile",
    "SymbolRecord",
]
