"""Java 语言适配器：基于 Tree-sitter 的静态结构、类型推导调用与 Spring 路由收集。

支持特性：
- ``package`` 声明与 ``import``（含单类导入、通配 ``*`` 导入、``import static``）；
- ``class`` / ``interface`` / ``enum`` / ``record``、继承/实现基类、Javadoc 注释；
- 构造器与方法签名、注解（decorators）；
- 基于字段声明与形参声明的静态类型推导（例如 ``userService.find()`` 解析为
  ``UserService.find``），大幅提升 Java 面向对象代码的静态调用边解析率；
- Spring Boot ``@RequestMapping`` + ``@GetMapping``/``@PostMapping`` 等路由组合，
  生成标准化 ``spring_route:/api/...`` 标记以供前后端跨端桥接。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
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

try:  # pragma: no branch - 运行时探测可选 Tree-sitter Java 依赖
    import tree_sitter
    import tree_sitter_java

    _JAVA_LANGUAGE = tree_sitter.Language(tree_sitter_java.language())
    TREE_SITTER_JAVA_AVAILABLE = True
except ImportError:  # pragma: no cover
    tree_sitter = None  # type: ignore[assignment]
    _JAVA_LANGUAGE = None
    TREE_SITTER_JAVA_AVAILABLE = False

_SPRING_MAPPING_NAMES = frozenset(
    {
        "RequestMapping",
        "GetMapping",
        "PostMapping",
        "PutMapping",
        "DeleteMapping",
        "PatchMapping",
    }
)
_STRING_LITERAL_RE = re.compile(r'"([^"]*)"')
_GENERIC_SUFFIX_RE = re.compile(r"<.*>$")


def java_module_name(relative_path: str, package_name: str | None) -> str:
    """结合 Java ``package`` 声明与文件名生成规范模块名。"""
    path = PurePosixPath(relative_path)
    stem = path.stem
    if package_name:
        return f"{package_name}.{stem}"
    parts = list(path.with_suffix("").parts)
    # 常见 Maven/Gradle 源码根目录裁剪，保留可读的包路径
    for marker in ("src", "main", "java"):
        if parts and parts[0] == marker:
            parts = parts[1:]
    return ".".join(parts) if parts else stem


def _node_text(node: Any, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )


def _strip_type_name(raw_type: str) -> str:
    """把 ``List<Order>`` / ``UserService[]`` 规范化为简单类名。"""
    cleaned = raw_type.strip().rstrip("[]").strip()
    cleaned = _GENERIC_SUFFIX_RE.sub("", cleaned).strip()
    if "." in cleaned:
        cleaned = cleaned.rsplit(".", 1)[-1]
    return cleaned


def _leading_javadoc(node: Any, source_bytes: bytes) -> str | None:
    prev = node.prev_named_sibling
    if prev is None or prev.type not in ("block_comment", "line_comment"):
        return None
    if node.start_point[0] - prev.end_point[0] > 1:
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
        return raw[2:-2].strip() or None
    if raw.startswith("//"):
        return raw.lstrip("/").strip() or None
    return None


def _extract_annotations(node: Any, source_bytes: bytes) -> list[str]:
    """从 modifiers 节点中提取 Java 注解文本列表。"""
    annotations: list[str] = []
    for child in node.named_children:
        if child.type != "modifiers":
            continue
        for mod in child.named_children:
            if mod.type in ("marker_annotation", "annotation"):
                text = " ".join(_node_text(mod, source_bytes).split())
                annotations.append(text)
    return annotations


def _extract_spring_route(annotations: list[str]) -> str | None:
    """从 Spring Mapping 注解中提取静态路径片段（如 ``/api/orders``）。"""
    for ann in annotations:
        name_part = ann.lstrip("@").split("(", 1)[0].strip()
        if name_part not in _SPRING_MAPPING_NAMES:
            continue
        match = _STRING_LITERAL_RE.search(ann)
        if match:
            return match.group(1).strip()
        return ""
    return None


def _join_routes(prefix: str | None, suffix: str | None) -> str | None:
    if prefix is None and suffix is None:
        return None
    p = (prefix or "").strip()
    s = (suffix or "").strip()
    if not p and not s:
        return None
    combined = f"/{p.strip('/')}/{s.strip('/')}".replace("//", "/")
    if len(combined) > 1:
        combined = combined.rstrip("/")
    return combined


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
                    message=f"Java 语法错误（{node.type}）",
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


class _JavaWalker:
    """遍历 Java AST，收集符号、import、类型推导调用与 Spring 路由。"""

    def __init__(
        self,
        module_symbol: SymbolRecord,
        source_bytes: bytes,
    ) -> None:
        self.module_symbol = module_symbol
        self.source_bytes = source_bytes
        self.symbols: list[SymbolRecord] = [module_symbol]
        self.imports: list[ImportRecord] = []
        self.calls: list[CallRecord] = []
        self.diagnostics: list[DiagnosticRecord] = []
        self._scope: list[SymbolRecord] = [module_symbol]
        self._type_scopes: list[dict[str, str]] = [{}]
        self._route_prefix_stack: list[str | None] = [None]
        self._used_ids: set[str] = {module_symbol.id}

    @property
    def current_scope(self) -> SymbolRecord:
        return self._scope[-1]

    def _lookup_var_type(self, var_name: str) -> str | None:
        for table in reversed(self._type_scopes):
            found = table.get(var_name)
            if found is not None:
                return found
        return None

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

        if node_type == "import_declaration":
            self._handle_import(node)
            return

        if node_type in (
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
        ):
            self._handle_type_declaration(node)
            return

        if node_type in ("method_declaration", "constructor_declaration"):
            self._handle_callable(node)
            return

        if node_type == "local_variable_declaration":
            self._record_variable_types(node)
            for child in node.named_children:
                self.walk(child)
            return

        if node_type == "method_invocation":
            self._handle_method_invocation(node)
            for child in node.named_children:
                self.walk(child)
            return

        if node_type == "object_creation_expression":
            self._handle_object_creation(node)
            for child in node.named_children:
                self.walk(child)
            return

        for child in node.named_children:
            self.walk(child)

    def _handle_import(self, node: Any) -> None:
        text = _node_text(node, self.source_bytes).strip().rstrip(";")
        is_static = "import static " in text
        has_asterisk = any(c.type == "asterisk" for c in node.named_children)
        ident_node = next(
            (
                c
                for c in node.named_children
                if c.type in ("scoped_identifier", "identifier")
            ),
            None,
        )
        if ident_node is None:
            return
        full_name = _node_text(ident_node, self.source_bytes).strip()
        if not full_name:
            return
        line = node.start_point[0] + 1
        col = node.start_point[1]

        if has_asterisk:
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=full_name,
                    name="*",
                    alias=None,
                    level=0,
                    line=line,
                    column=col,
                )
            )
            return

        if is_static and "." in full_name:
            # import static com.foo.Util.helper -> module=com.foo.Util, name=helper
            mod, member = full_name.rsplit(".", 1)
            self.imports.append(
                ImportRecord(
                    file_path=self.module_symbol.file_path,
                    module=mod,
                    name=member,
                    alias=None,
                    level=0,
                    line=line,
                    column=col,
                )
            )
            return

        simple_name = full_name.rsplit(".", 1)[-1]
        self.imports.append(
            ImportRecord(
                file_path=self.module_symbol.file_path,
                module=full_name,
                name=simple_name,
                alias=None,
                level=0,
                line=line,
                column=col,
            )
        )

    def _handle_type_declaration(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, self.source_bytes)
        parent = self.current_scope
        qualified_name = f"{parent.qualified_name}.{name}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        annotations = _extract_annotations(node, self.source_bytes)
        class_route = _extract_spring_route(annotations)

        bases: list[str] = []
        superclass = node.child_by_field_name("superclass")
        if superclass is not None:
            for c in superclass.named_children:
                bases.append(_strip_type_name(_node_text(c, self.source_bytes)))
        interfaces = node.child_by_field_name("interfaces")
        if interfaces is not None:
            for c in interfaces.named_children:
                if c.type == "type_list":
                    for item in c.named_children:
                        bases.append(
                            _strip_type_name(_node_text(item, self.source_bytes))
                        )
                else:
                    bases.append(_strip_type_name(_node_text(c, self.source_bytes)))

        keyword = node.type.split("_", 1)[0]
        sig = (
            f"{keyword} {name} extends {', '.join(bases)}"
            if bases
            else f"{keyword} {name}"
        )

        symbol = SymbolRecord(
            id=self._make_id(f"class:{qualified_name}", start_line),
            kind="class",
            name=name,
            qualified_name=qualified_name,
            file_path=self.module_symbol.file_path,
            start_line=start_line,
            end_line=end_line,
            signature=sig,
            docstring=_leading_javadoc(node, self.source_bytes),
            decorators=annotations,
            bases=bases,
            parent_id=parent.id,
            language="java",
        )
        self.symbols.append(symbol)
        self._scope.append(symbol)
        self._type_scopes.append({})
        self._route_prefix_stack.append(class_route)

        body = node.child_by_field_name("body")
        if body is not None:
            # 第一遍：先收集所有字段类型（确保构造器/方法体能解析 this.field / field）
            for child in body.named_children:
                if child.type == "field_declaration":
                    self._record_variable_types(child)
            # 第二遍：遍历方法、构造器与内部类
            for child in body.named_children:
                if child.type != "field_declaration":
                    self.walk(child)

        self._route_prefix_stack.pop()
        self._type_scopes.pop()
        self._scope.pop()

    def _record_variable_types(self, node: Any) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is None:
            return
        type_name = _strip_type_name(_node_text(type_node, self.source_bytes))
        if not type_name or not type_name[0].isupper():
            return
        for child in node.named_children:
            if child.type == "variable_declarator":
                var_name_node = child.child_by_field_name("name")
                if var_name_node is not None:
                    self._type_scopes[-1][
                        _node_text(var_name_node, self.source_bytes)
                    ] = type_name

    def _handle_callable(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, self.source_bytes)
        parent = self.current_scope
        qualified_name = f"{parent.qualified_name}.{name}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        annotations = _extract_annotations(node, self.source_bytes)

        # 合并类级 @RequestMapping 与方法级 @GetMapping/@PostMapping
        method_route = _extract_spring_route(annotations)
        if method_route is not None:
            full_route = _join_routes(self._route_prefix_stack[-1], method_route)
            if full_route:
                annotations.append(f"spring_route:{full_route}")

        ret_node = node.child_by_field_name("type")
        ret_text = (
            _node_text(ret_node, self.source_bytes).strip()
            if ret_node is not None
            else ""
        )
        params_node = node.child_by_field_name("parameters")
        params_text = (
            " ".join(_node_text(params_node, self.source_bytes).split())
            if params_node is not None
            else "()"
        )
        if len(params_text) > 120:
            params_text = params_text[:117] + "...)"
        signature = f"{ret_text} {name}{params_text}".strip()

        symbol = SymbolRecord(
            id=self._make_id(f"method:{qualified_name}", start_line),
            kind="method",
            name=name,
            qualified_name=qualified_name,
            file_path=self.module_symbol.file_path,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            docstring=_leading_javadoc(node, self.source_bytes),
            decorators=annotations,
            parent_id=parent.id,
            language="java",
        )
        self.symbols.append(symbol)
        self._scope.append(symbol)
        self._type_scopes.append({})

        # 收集方法形参静态类型
        if params_node is not None:
            for param in params_node.named_children:
                if param.type in ("formal_parameter", "spread_parameter"):
                    p_type = param.child_by_field_name("type")
                    p_name = param.child_by_field_name("name")
                    if p_type is not None and p_name is not None:
                        t_name = _strip_type_name(
                            _node_text(p_type, self.source_bytes)
                        )
                        if t_name and t_name[0].isupper():
                            self._type_scopes[-1][
                                _node_text(p_name, self.source_bytes)
                            ] = t_name

        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                self.walk(child)

        self._type_scopes.pop()
        self._scope.pop()

    def _handle_method_invocation(self, node: Any) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        method_name = _node_text(name_node, self.source_bytes)
        obj_node = node.child_by_field_name("object")
        line = node.start_point[0] + 1
        col = node.start_point[1]

        if obj_node is None:
            # 类内隐式 this 调用：helper(...)
            callee_expr = method_name
            callee_name = f"this.{method_name}"
        else:
            obj_text = _node_text(obj_node, self.source_bytes).strip()
            callee_expr = f"{obj_text}.{method_name}"
            if obj_text == "this":
                callee_name = f"this.{method_name}"
            else:
                # 处理 this.userService.find() -> 查找 userService 类型
                lookup_key = (
                    obj_text[5:] if obj_text.startswith("this.") else obj_text
                )
                inferred_type = self._lookup_var_type(lookup_key)
                if inferred_type is not None:
                    callee_name = f"{inferred_type}.{method_name}"
                else:
                    callee_name = f"{obj_text}.{method_name}"

        self.calls.append(
            CallRecord(
                caller_id=self.current_scope.id,
                callee_expression=callee_expr[:120],
                callee_name=callee_name[:120],
                line=line,
                column=col,
            )
        )

    def _handle_object_creation(self, node: Any) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is None:
            return
        class_name = _strip_type_name(_node_text(type_node, self.source_bytes))
        if not class_name:
            return
        self.calls.append(
            CallRecord(
                caller_id=self.current_scope.id,
                callee_expression=f"new {class_name}()",
                callee_name=class_name,
                line=node.start_point[0] + 1,
                column=node.start_point[1],
            )
        )


class JavaTreeSitterParser(LanguageAdapter):
    """基于 Tree-sitter 的 Java 静态解析器。"""

    language = "java"
    extensions = (".java",)

    def __init__(self) -> None:
        if not TREE_SITTER_JAVA_AVAILABLE:  # pragma: no cover
            raise RuntimeError(
                "缺少 tree-sitter / tree-sitter-java 依赖，无法启用 Java 解析器。"
            )
        self._parser = tree_sitter.Parser(_JAVA_LANGUAGE)

    def parse(self, source: SourceFile) -> ParsedFile:
        source_bytes = source.text.encode("utf-8")
        tree = self._parser.parse(source_bytes)
        root = tree.root_node

        if root.has_error:
            diagnostics = _collect_syntax_errors(root, source.relative_path)
            if not diagnostics:
                diagnostics = [
                    DiagnosticRecord(
                        code="syntax_error",
                        message="Java 语法解析失败",
                        severity="error",
                        file_path=source.relative_path,
                        line=1,
                        column=0,
                    )
                ]
            return ParsedFile(diagnostics=diagnostics)

        package_name: str | None = None
        for child in root.named_children:
            if child.type == "package_declaration":
                pkg_ident = next(
                    (
                        c
                        for c in child.named_children
                        if c.type in ("scoped_identifier", "identifier")
                    ),
                    None,
                )
                if pkg_ident is not None:
                    package_name = _node_text(pkg_ident, source_bytes).strip()
                break

        module_name = java_module_name(source.relative_path, package_name)
        module_symbol = SymbolRecord(
            id=f"module:{module_name}",
            kind="module",
            name=module_name,
            qualified_name=module_name,
            file_path=source.relative_path,
            start_line=1,
            end_line=max(1, source.line_count),
            parent_id=None,
            language=self.language,
        )
        walker = _JavaWalker(module_symbol, source_bytes)
        for child in root.named_children:
            walker.walk(child)
        return ParsedFile(
            symbols=walker.symbols,
            imports=walker.imports,
            calls=walker.calls,
            diagnostics=walker.diagnostics,
        )


__all__ = [
    "TREE_SITTER_JAVA_AVAILABLE",
    "JavaTreeSitterParser",
    "java_module_name",
]
