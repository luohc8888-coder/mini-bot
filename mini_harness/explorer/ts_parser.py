"""TypeScript / TSX 语言适配器：基于 Tree-sitter 的静态结构、JSX 组件与 API 路由收集。

本适配器实现 ``LanguageAdapter`` 协议，输出与 Python 适配器完全一致的
``ParsedFile``（``SymbolRecord`` / ``ImportRecord`` / ``CallRecord`` /
``DiagnosticRecord``），供 ``CodeGraph``、SQLite 混合检索与架构分析统一消费。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from .base import LanguageAdapter
from .models import (
    CallRecord,
    DiagnosticRecord,
    ImportRecord,
    ParsedFile,
    SourceFile,
    SymbolRecord,
)

try:  # pragma: no branch - 运行时探测可选 Tree-sitter 依赖
    import tree_sitter
    import tree_sitter_typescript

    _TS_LANGUAGE = tree_sitter.Language(
        tree_sitter_typescript.language_typescript()
    )
    _TSX_LANGUAGE = tree_sitter.Language(
        tree_sitter_typescript.language_tsx()
    )
    TREE_SITTER_TS_AVAILABLE = True
except ImportError:  # pragma: no cover
    tree_sitter = None  # type: ignore[assignment]
    _TS_LANGUAGE = None
    _TSX_LANGUAGE = None
    TREE_SITTER_TS_AVAILABLE = False

_API_ROUTE_RE = re.compile(r"(/api/[A-Za-z0-9_./-]+)")
_JSX_COMPONENT_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")


def ts_module_name_from_relative_path(relative_path: str) -> str:
    """将 ``frontend/src/pages/ExplorerPage.tsx`` 转换为规范模块名。"""
    path = PurePosixPath(relative_path)
    name = path.name
    if name.endswith(".d.ts"):
        stem = name[:-5]
        parts = [*path.parts[:-1], stem] if stem else list(path.parts[:-1])
    else:
        parts = list(path.with_suffix("").parts)
    if not parts:
        return path.stem
    return ".".join(parts)


def _node_text(node: Any, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"', "`"):
        return text[1:-1]
    return text


def _leading_jsdoc(node: Any, source_bytes: bytes) -> str | None:
    """提取紧邻声明之前的 JSDoc / 单行注释作为 docstring。"""
    target = node
    if node.parent is not None and node.parent.type == "export_statement":
        target = node.parent
    prev = target.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    # 只接受紧贴声明上方（最多隔 1 个空行）的注释
    if target.start_point[0] - prev.end_point[0] > 1:
        return None
    raw = _node_text(prev, source_bytes).strip()
    if raw.startswith("/**") and raw.endswith("*/"):
        lines = [
            re.sub(r"^\s*\*\s?", "", line).strip()
            for line in raw[3:-2].splitlines()
        ]
        cleaned = "\n".join(line for line in lines if line)
        return cleaned or None
    if raw.startswith("/*") and raw.endswith("*/"):
        cleaned = raw[2:-2].strip()
        return cleaned or None
    if raw.startswith("//"):
        cleaned = raw.lstrip("/").strip()
        return cleaned or None
    return None


def _compact_signature(prefix: str, name: str, params_text: str, return_text: str) -> str:
    params = " ".join(params_text.split())
    if len(params) > 120:
        params = params[:117] + "...)"
    ret = " ".join(return_text.split())
    if len(ret) > 60:
        ret = ret[:57] + "..."
    return f"{prefix} {name}{params}{ret}".strip()


def _extract_api_routes(arguments_node: Any, source_bytes: bytes) -> list[str]:
    """从函数调用参数中提取静态 ``/api/...`` 路径（用于前后端跨端桥接）。"""
    routes: list[str] = []
    for child in arguments_node.named_children:
        if child.type not in ("string", "template_string"):
            continue
        raw = _strip_quotes(_node_text(child, source_bytes))
        # 去掉模板插值与 query string，只保留静态路由前缀
        static_prefix = raw.split("?", 1)[0].split("${", 1)[0].rstrip("/")
        match = _API_ROUTE_RE.match(static_prefix)
        if match:
            route = match.group(1)
            if route not in routes:
                routes.append(route)
    return routes


class _TsWalker:
    """递归遍历 Tree-sitter 语法树并收集结构化符号、import 与调用。"""

    def __init__(
        self,
        module_symbol: SymbolRecord,
        source_bytes: bytes,
        *,
        language: str = "typescript",
    ) -> None:
        self.module_symbol = module_symbol
        self.source_bytes = source_bytes
        self.language = language
        self.symbols: list[SymbolRecord] = [module_symbol]
        self.imports: list[ImportRecord] = []
        self.calls: list[CallRecord] = []
        self.diagnostics: list[DiagnosticRecord] = []
        self._scope: list[SymbolRecord] = [module_symbol]
        self._used_ids: set[str] = {module_symbol.id}

    @property
    def current_scope(self) -> SymbolRecord:
        return self._scope[-1]

    def _make_id(self, base: str, line: int) -> str:
        if base not in self._used_ids:
            self._used_ids.add(base)
            return base
        candidate = f"{base}@{line}"
        suffix = 2
        while candidate in self._used_ids:
            candidate = f"{base}@{line}-{suffix}"
            suffix += 1
        self._used_ids.add(candidate)
        return candidate

    def walk(self, node: Any) -> None:
        node_type = node.type

        if node_type == "import_statement":
            self._handle_import(node)
            return

        if node_type == "export_statement":
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                self._handle_reexport(node, source_node)
                return
            for child in node.named_children:
                self.walk(child)
            return

        if node_type in ("class_declaration", "abstract_class_declaration"):
            self._handle_class(node)
            return

        if node_type == "interface_declaration":
            self._handle_interface(node)
            return

        if node_type in ("function_declaration", "generator_function_declaration"):
            self._handle_function(node)
            return

        if node_type == "method_definition":
            self._handle_method(node)
            return

        if node_type in ("lexical_declaration", "variable_declaration"):
            self._handle_variable_declaration(node)
            return

        if node_type == "call_expression":
            self._handle_call(node)
            # 继续遍历参数与嵌套调用
            for child in node.named_children:
                self.walk(child)
            return

        if node_type in ("jsx_self_closing_element", "jsx_opening_element"):
            self._handle_jsx_component(node)
            for child in node.named_children:
                self.walk(child)
            return

        for child in node.named_children:
            self.walk(child)

    def _handle_import(self, node: Any) -> None:
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return
        specifier = _strip_quotes(_node_text(source_node, self.source_bytes))
        if not specifier:
            return
        line = node.start_point[0] + 1
        col = node.start_point[1]
        level = 1 if specifier.startswith(".") else 0

        clause = next(
            (c for c in node.named_children if c.type == "import_clause"),
            None,
        )
        if clause is None:
            # 副作用导入：import "./index.css"
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=specifier,
                    name=None,
                    alias=None,
                    level=level,
                    line=line,
                    column=col,
                )
            )
            return

        added = False
        for child in clause.named_children:
            if child.type == "identifier":
                local_name = _node_text(child, self.source_bytes)
                self.imports.append(
                    ImportRecord(
                        file_path=self.module_symbol.file_path,
                        module=specifier,
                        name="default",
                        alias=local_name,
                        level=level,
                        line=line,
                        column=col,
                    )
                )
                added = True
            elif child.type == "namespace_import":
                ident = next(
                    (c for c in child.named_children if c.type == "identifier"),
                    None,
                )
                alias = _node_text(ident, self.source_bytes) if ident else None
                self.imports.append(
                    ImportRecord(
                        file_path=self.module_symbol.file_path,
                        module=specifier,
                        name="*",
                        alias=alias,
                        level=level,
                        line=line,
                        column=col,
                    )
                )
                added = True
            elif child.type == "named_imports":
                for spec in child.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name_node = spec.child_by_field_name("name")
                    alias_node = spec.child_by_field_name("alias")
                    if name_node is None:
                        continue
                    imported_name = _node_text(name_node, self.source_bytes)
                    alias_name = (
                        _node_text(alias_node, self.source_bytes)
                        if alias_node is not None
                        else None
                    )
                    self.imports.append(
                        ImportRecord(
                            file_path=self.module_symbol.file_path,
                            module=specifier,
                            name=imported_name,
                            alias=alias_name,
                            level=level,
                            line=line,
                            column=col,
                        )
                    )
                    added = True

        if not added:
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=specifier,
                    name=None,
                    alias=None,
                    level=level,
                    line=line,
                    column=col,
                )
            )

    def _handle_reexport(self, node: Any, source_node: Any) -> None:
        specifier = _strip_quotes(_node_text(source_node, self.source_bytes))
        if not specifier:
            return
        line = node.start_point[0] + 1
        col = node.start_point[1]
        level = 1 if specifier.startswith(".") else 0

        export_clause = next(
            (c for c in node.named_children if c.type == "export_clause"),
            None,
        )
        if export_clause is None:
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=specifier,
                    name="*",
                    alias=None,
                    level=level,
                    line=line,
                    column=col,
                )
            )
            return

        for spec in export_clause.named_children:
            if spec.type != "export_specifier":
                continue
            name_node = spec.child_by_field_name("name")
            alias_node = spec.child_by_field_name("alias")
            if name_node is None:
                continue
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=specifier,
                    name=_node_text(name_node, self.source_bytes),
                    alias=(
                        _node_text(alias_node, self.source_bytes)
                        if alias_node is not None
                        else None
                    ),
                    level=level,
                    line=line,
                    column=col,
                )
            )

    def _handle_class(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, self.source_bytes)
        parent = self.current_scope
        qualified_name = f"{parent.qualified_name}.{name}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        bases: list[str] = []
        for child in node.named_children:
            if child.type == "class_heritage":
                for clause in child.named_children:
                    if clause.type == "extends_clause":
                        val = clause.child_by_field_name("value")
                        if val is not None:
                            bases.append(_node_text(val, self.source_bytes))
                    elif clause.type == "implements_clause":
                        for impl in clause.named_children:
                            bases.append(_node_text(impl, self.source_bytes))

        sig = f"class {name} extends {', '.join(bases)}" if bases else f"class {name}"
        symbol = SymbolRecord(
            id=self._make_id(f"class:{qualified_name}", start_line),
            kind="class",
            name=name,
            qualified_name=qualified_name,
            file_path=self.module_symbol.file_path,
            start_line=start_line,
            end_line=end_line,
            signature=sig,
            docstring=_leading_jsdoc(node, self.source_bytes),
            bases=bases,
            parent_id=parent.id,
            language=self.language,
        )
        self.symbols.append(symbol)
        self._scope.append(symbol)
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                self.walk(child)
        self._scope.pop()

    def _handle_interface(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, self.source_bytes)
        parent = self.current_scope
        qualified_name = f"{parent.qualified_name}.{name}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        symbol = SymbolRecord(
            id=self._make_id(f"class:{qualified_name}", start_line),
            kind="class",
            name=name,
            qualified_name=qualified_name,
            file_path=self.module_symbol.file_path,
            start_line=start_line,
            end_line=end_line,
            signature=f"interface {name}",
            docstring=_leading_jsdoc(node, self.source_bytes),
            decorators=["interface"],
            parent_id=parent.id,
            language=self.language,
        )
        self.symbols.append(symbol)

    def _handle_function(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, self.source_bytes)
        parent = self.current_scope
        kind = "method" if parent.kind == "class" else "function"
        qualified_name = f"{parent.qualified_name}.{name}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        params_node = node.child_by_field_name("parameters")
        return_node = node.child_by_field_name("return_type")
        params_text = (
            _node_text(params_node, self.source_bytes)
            if params_node is not None
            else "()"
        )
        return_text = (
            _node_text(return_node, self.source_bytes)
            if return_node is not None
            else ""
        )
        raw_head = _node_text(node, self.source_bytes).lstrip()
        prefix = "async function" if raw_head.startswith("async ") else "function"

        symbol = SymbolRecord(
            id=self._make_id(f"{kind}:{qualified_name}", start_line),
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            file_path=self.module_symbol.file_path,
            start_line=start_line,
            end_line=end_line,
            signature=_compact_signature(prefix, name, params_text, return_text),
            docstring=_leading_jsdoc(node, self.source_bytes),
            parent_id=parent.id,
            language=self.language,
        )
        self.symbols.append(symbol)
        self._scope.append(symbol)
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                self.walk(child)
        self._scope.pop()

    def _handle_method(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, self.source_bytes)
        parent = self.current_scope
        qualified_name = f"{parent.qualified_name}.{name}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        params_node = node.child_by_field_name("parameters")
        return_node = node.child_by_field_name("return_type")
        params_text = (
            _node_text(params_node, self.source_bytes)
            if params_node is not None
            else "()"
        )
        return_text = (
            _node_text(return_node, self.source_bytes)
            if return_node is not None
            else ""
        )

        symbol = SymbolRecord(
            id=self._make_id(f"method:{qualified_name}", start_line),
            kind="method",
            name=name,
            qualified_name=qualified_name,
            file_path=self.module_symbol.file_path,
            start_line=start_line,
            end_line=end_line,
            signature=_compact_signature("method", name, params_text, return_text),
            docstring=_leading_jsdoc(node, self.source_bytes),
            parent_id=parent.id,
            language=self.language,
        )
        self.symbols.append(symbol)
        self._scope.append(symbol)
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                self.walk(child)
        self._scope.pop()

    def _handle_variable_declaration(self, node: Any) -> None:
        # 只把模块顶层或类顶层的箭头函数 / 函数表达式提升为符号，
        # 函数内部的局部回调（如 useCallback）保持归属当前作用域以保留调用边。
        is_top_scope = self.current_scope.kind in ("module", "class")
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                self.walk(declarator)
                continue
            name_node = declarator.child_by_field_name("name")
            value_node = declarator.child_by_field_name("value")
            if (
                is_top_scope
                and value_node is not None
                and value_node.type == "call_expression"
                and self._try_handle_commonjs_require(declarator, name_node, value_node)
            ):
                continue
            if (
                is_top_scope
                and name_node is not None
                and name_node.type == "identifier"
                and value_node is not None
                and value_node.type in ("arrow_function", "function_expression")
            ):
                name = _node_text(name_node, self.source_bytes)
                parent = self.current_scope
                kind = "method" if parent.kind == "class" else "function"
                qualified_name = f"{parent.qualified_name}.{name}"
                start_line = node.start_point[0] + 1
                end_line = declarator.end_point[0] + 1
                params_node = value_node.child_by_field_name("parameters")
                return_node = value_node.child_by_field_name("return_type")
                params_text = (
                    _node_text(params_node, self.source_bytes)
                    if params_node is not None
                    else "()"
                )
                return_text = (
                    _node_text(return_node, self.source_bytes)
                    if return_node is not None
                    else ""
                )
                symbol = SymbolRecord(
                    id=self._make_id(f"{kind}:{qualified_name}", start_line),
                    kind=kind,
                    name=name,
                    qualified_name=qualified_name,
                    file_path=self.module_symbol.file_path,
                    start_line=start_line,
                    end_line=end_line,
                    signature=_compact_signature(
                        "const", name, params_text, return_text
                    ),
                    docstring=_leading_jsdoc(node, self.source_bytes),
                    parent_id=parent.id,
                    language="typescript",
                )
                self.symbols.append(symbol)
                self._scope.append(symbol)
                body = value_node.child_by_field_name("body")
                if body is not None:
                    self.walk(body)
                self._scope.pop()
            elif value_node is not None:
                self.walk(value_node)

    def _try_handle_commonjs_require(
        self,
        declarator: Any,
        name_node: Any,
        call_node: Any,
    ) -> bool:
        """识别 CommonJS ``const x = require('./mod')`` 与解构导入。"""
        func_node = call_node.child_by_field_name("function")
        if func_node is None or _node_text(func_node, self.source_bytes) != "require":
            return False
        args_node = call_node.child_by_field_name("arguments")
        if args_node is None or not args_node.named_children:
            return False
        first_arg = args_node.named_children[0]
        if first_arg.type not in ("string", "template_string"):
            return False
        specifier = _strip_quotes(_node_text(first_arg, self.source_bytes))
        if not specifier:
            return False
        line = declarator.start_point[0] + 1
        col = declarator.start_point[1]
        level = 1 if specifier.startswith(".") else 0

        if name_node is not None and name_node.type == "identifier":
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=specifier,
                    name="default",
                    alias=_node_text(name_node, self.source_bytes),
                    level=level,
                    line=line,
                    column=col,
                )
            )
            return True
        if name_node is not None and name_node.type == "object_pattern":
            for prop in name_node.named_children:
                if prop.type == "shorthand_property_identifier_pattern":
                    ident = _node_text(prop, self.source_bytes)
                    self.imports.append(
                        ImportRecord(
                            file_path=self.module_symbol.file_path,
                            module=specifier,
                            name=ident,
                            alias=None,
                            level=level,
                            line=line,
                            column=col,
                        )
                    )
                elif prop.type == "pair_pattern":
                    key = prop.child_by_field_name("key")
                    val = prop.child_by_field_name("value")
                    if key is not None and val is not None:
                        self.imports.append(
                            ImportRecord(
                                file_path=self.module_symbol.file_path,
                                module=specifier,
                                name=_node_text(key, self.source_bytes),
                                alias=_node_text(val, self.source_bytes),
                                level=level,
                                line=line,
                                column=col,
                            )
                        )
            return True
        return False

    def _handle_call(self, node: Any) -> None:
        func_node = node.child_by_field_name("function")
        if func_node is None:
            return
        line = node.start_point[0] + 1
        col = node.start_point[1]
        callee_expr = _node_text(func_node, self.source_bytes).strip()
        if len(callee_expr) > 120:
            callee_expr = callee_expr[:120]

        if func_node.type in ("identifier", "member_expression"):
            self.calls.append(
                CallRecord(
                    caller_id=self.current_scope.id,
                    callee_expression=callee_expr,
                    callee_name=callee_expr,
                    line=line,
                    column=col,
                )
            )

        # 提取前后端跨端 /api/... 静态路由调用（例如 fetch("/api/graph")、requestJson("/api/chat")）
        args_node = node.child_by_field_name("arguments")
        if args_node is not None:
            for route in _extract_api_routes(args_node, self.source_bytes):
                self.calls.append(
                    CallRecord(
                        caller_id=self.current_scope.id,
                        callee_expression=f'HTTP "{route}"',
                        callee_name=f"http_api:{route}",
                        line=line,
                        column=col,
                    )
                )

    def _handle_jsx_component(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        tag_name = _node_text(name_node, self.source_bytes).strip()
        if not tag_name:
            return
        root_name = tag_name.split(".", 1)[0]
        if not _JSX_COMPONENT_RE.match(root_name):
            return
        self.calls.append(
            CallRecord(
                caller_id=self.current_scope.id,
                callee_expression=f"<{tag_name} />",
                callee_name=tag_name,
                line=node.start_point[0] + 1,
                column=node.start_point[1],
            )
        )


def _collect_syntax_errors(
    root: Any, file_path: str
) -> list[DiagnosticRecord]:
    errors: list[DiagnosticRecord] = []
    stack = [root]
    while stack and len(errors) < 10:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            errors.append(
                DiagnosticRecord(
                    code="syntax_error",
                    message=f"TypeScript 语法错误（{node.type}）",
                    severity="error",
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                    column=node.start_point[1],
                )
            )
            continue
        if node.has_error:
            stack.extend(reversed(node.children))
    return errors


class TypeScriptTreeSitterParser(LanguageAdapter):
    """基于 Tree-sitter 的 TypeScript / TSX 静态解析器。"""

    language = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts")

    def __init__(self) -> None:
        if not TREE_SITTER_TS_AVAILABLE:  # pragma: no cover
            raise RuntimeError(
                "缺少 tree-sitter / tree-sitter-typescript 依赖，无法启用 TS 解析器。"
            )
        self._ts_parser = tree_sitter.Parser(_TS_LANGUAGE)
        self._tsx_parser = tree_sitter.Parser(_TSX_LANGUAGE)

    def parse(self, source: SourceFile) -> ParsedFile:
        """解析单个 TypeScript / TSX 源文件。"""
        source_bytes = source.text.encode("utf-8")
        is_tsx = Path(source.relative_path).suffix.lower() in (
            ".tsx",
            ".jsx",
            ".js",
            ".mjs",
            ".cjs",
        )
        parser = self._tsx_parser if is_tsx else self._ts_parser
        tree = parser.parse(source_bytes)
        root = tree.root_node

        if root.has_error:
            diagnostics = _collect_syntax_errors(root, source.relative_path)
            if not diagnostics:
                diagnostics = [
                    DiagnosticRecord(
                        code="syntax_error",
                        message="TypeScript 语法解析失败",
                        severity="error",
                        file_path=source.relative_path,
                        line=1,
                        column=0,
                    )
                ]
            return ParsedFile(diagnostics=diagnostics)

        module_name = ts_module_name_from_relative_path(source.relative_path)
        end_line = max(1, source.line_count)
        first_child = root.named_children[0] if root.named_children else None
        module_doc: str | None = None
        if first_child is not None and first_child.type == "comment":
            raw = _node_text(first_child, source_bytes).strip()
            if raw.startswith("/**") and raw.endswith("*/"):
                lines = [
                    re.sub(r"^\s*\*\s?", "", line).strip()
                    for line in raw[3:-2].splitlines()
                ]
                module_doc = "\n".join(line for line in lines if line) or None

        module_symbol = SymbolRecord(
            id=f"module:{module_name}",
            kind="module",
            name=module_name,
            qualified_name=module_name,
            file_path=source.relative_path,
            start_line=1,
            end_line=end_line,
            docstring=module_doc,
            parent_id=None,
            language=self.language,
        )
        walker = _TsWalker(module_symbol, source_bytes, language=self.language)
        for child in root.named_children:
            walker.walk(child)
        return ParsedFile(
            symbols=walker.symbols,
            imports=walker.imports,
            calls=walker.calls,
            diagnostics=walker.diagnostics,
        )


class JavaScriptTreeSitterParser(TypeScriptTreeSitterParser):
    """基于 Tree-sitter (TSX 兼容文法) 的 JavaScript / JSX 静态解析器。"""

    language = "javascript"
    extensions = (".js", ".jsx", ".mjs", ".cjs")


__all__ = [
    "TREE_SITTER_TS_AVAILABLE",
    "JavaScriptTreeSitterParser",
    "TypeScriptTreeSitterParser",
    "ts_module_name_from_relative_path",
]
