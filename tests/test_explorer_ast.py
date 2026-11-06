"""Python AST 适配器测试。"""

from __future__ import annotations

from mini_harness.explorer import PythonAstParser, SourceFile
from mini_harness.explorer.ast_parser import module_name_from_relative_path


def _source(relative_path: str, text: str) -> SourceFile:
    return SourceFile(
        path=f"/tmp/{relative_path}",
        relative_path=relative_path,
        size=len(text.encode("utf-8")),
        sha256="test",
        text=text,
        line_count=len(text.splitlines()),
    )


SERVICE_SOURCE = '''"""Service module."""
from __future__ import annotations
import os as operating_system
from .models import Item as Model


class Base:
    pass


@decorator
class Service(Base):
    """Service docs."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, value: int = 1) -> str:
        helper(value)
        return str(value)


def helper(value: int) -> None:
    print(value)
'''


def test_module_name_from_relative_path() -> None:
    """包初始化文件应得到包名而不是 ``pkg.__init__``。"""
    assert module_name_from_relative_path("pkg/__init__.py") == "pkg"
    assert module_name_from_relative_path("pkg/service.py") == "pkg.service"
    assert module_name_from_relative_path("__init__.py") == "__init__"


def test_python_parser_extracts_symbols_imports_and_calls() -> None:
    """解析器应提取类、方法、函数、签名、行号、import 和原始调用。"""
    parsed = PythonAstParser().parse(_source("pkg/service.py", SERVICE_SOURCE))

    assert not parsed.diagnostics
    symbols = {symbol.qualified_name: symbol for symbol in parsed.symbols}
    assert symbols["pkg.service"].kind == "module"
    assert symbols["pkg.service"].docstring == "Service module."
    assert symbols["pkg.service.Base"].kind == "class"
    assert symbols["pkg.service.Service"].kind == "class"
    assert symbols["pkg.service.Service"].bases == ["Base"]
    assert symbols["pkg.service.Service"].decorators == ["decorator"]
    assert symbols["pkg.service.Service"].start_line == 12
    assert symbols["pkg.service.Service"].end_line == 20
    assert symbols["pkg.service.Service.__init__"].kind == "method"
    assert symbols["pkg.service.Service.__init__"].parent_id == (
        "class:pkg.service.Service"
    )
    assert symbols["pkg.service.Service.run"].kind == "method"
    assert symbols["pkg.service.Service.run"].start_line == 18
    assert symbols["pkg.service.Service.run"].end_line == 20
    assert symbols["pkg.service.Service.run"].signature.startswith(
        "async def run("
    )
    assert "value: int" in symbols["pkg.service.Service.run"].signature
    assert symbols["pkg.service.helper"].kind == "function"

    imports = {(item.module, item.name, item.alias, item.level) for item in parsed.imports}
    assert ("os", None, "operating_system", 0) in imports
    assert ("models", "Item", "Model", 1) in imports
    assert ("__future__", "annotations", None, 0) in imports

    helper_calls = [
        call
        for call in parsed.calls
        if call.caller_id == "method:pkg.service.Service.run"
        and call.callee_name == "helper"
    ]
    assert len(helper_calls) == 1
    assert helper_calls[0].line == 19
    assert any(
        call.callee_name == "print"
        and call.caller_id == "function:pkg.service.helper"
        for call in parsed.calls
    )


def test_python_parser_handles_nested_classes_and_functions() -> None:
    """嵌套类/函数仍需有唯一限定名和正确的父节点。"""
    text = """class Outer:
    class Inner:
        def method(self):
            return 1

    def outer_method(self):
        def inner():
            return 2
        return inner()
"""
    parsed = PythonAstParser().parse(_source("nested.py", text))
    symbols = {symbol.qualified_name: symbol for symbol in parsed.symbols}
    assert symbols["nested.Outer.Inner"].kind == "class"
    assert symbols["nested.Outer.Inner.method"].kind == "method"
    assert symbols["nested.Outer.outer_method"].kind == "method"
    assert symbols["nested.Outer.outer_method.inner"].kind == "function"
    assert symbols["nested.Outer.outer_method.inner"].parent_id == (
        "method:nested.Outer.outer_method"
    )


def test_python_parser_returns_diagnostic_for_syntax_error() -> None:
    """语法错误必须以诊断返回，不能抛异常中断整个项目。"""
    parsed = PythonAstParser().parse(
        _source("bad.py", "def broken(:\n    pass\n")
    )
    assert parsed.symbols == []
    assert len(parsed.diagnostics) == 1
    assert parsed.diagnostics[0].code == "syntax_error"
    assert parsed.diagnostics[0].file_path == "bad.py"


def test_python_parser_empty_file_has_module_symbol() -> None:
    """空文件仍应产生一个可定位的模块节点。"""
    parsed = PythonAstParser().parse(_source("empty.py", ""))
    assert len(parsed.symbols) == 1
    assert parsed.symbols[0].kind == "module"
    assert parsed.symbols[0].start_line == 1
    assert parsed.symbols[0].end_line == 1
    assert not parsed.diagnostics
