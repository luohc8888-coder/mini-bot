"""ProjectScanner 文件发现、过滤、诊断与确定性测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.explorer import (
    LanguageAdapter,
    ParsedFile,
    ProjectScanner,
    PythonAstParser,
    SymbolRecord,
    scan_project,
)
from mini_harness.workspace import WorkspaceResolver


def _workspace(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    return WorkspaceResolver(cwd=tmp_path).resolve(root), root


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_scanner_indexes_python_files_and_honors_ignore_dirs(tmp_path: Path) -> None:
    """扫描器只索引源码，不进入 .git、node_modules 等噪声目录。"""
    workspace, root = _workspace(tmp_path)
    _write(root, "main.py", "def main():\n    return 1\n")
    _write(root, "pkg/__init__.py", '"""Package."""\n')
    _write(root, "pkg/service.py", "class Service:\n    def run(self):\n        pass\n")
    _write(root, ".git/hidden.py", "def hidden():\n    pass\n")
    _write(root, "node_modules/pkg/ignored.py", "def ignored():\n    pass\n")
    _write(root, "dist/generated.py", "def generated():\n    pass\n")
    _write(root, "notes.txt", "not python")

    index = scan_project(workspace)

    paths = {record.path for record in index.files}
    assert paths == {"main.py", "pkg/__init__.py", "pkg/service.py"}
    assert {record.language for record in index.files} == {"python"}
    assert index.stats["indexedFiles"] == 3
    assert index.stats["classes"] == 1
    assert index.stats["methods"] == 1
    assert not index.diagnostics


def test_scanner_skips_large_files_and_records_diagnostic(tmp_path: Path) -> None:
    """超过大小限制的文件不能被读取为符号，但必须留下 skipped 记录。"""
    workspace, root = _workspace(tmp_path)
    _write(root, "small.py", "x = 1\n")
    _write(root, "large.py", "x = '" + ("a" * 200) + "'\n")

    index = scan_project(workspace, max_file_bytes=80)
    records = {record.path: record for record in index.files}
    assert records["small.py"].parse_status == "ok"
    assert records["large.py"].parse_status == "skipped"
    assert "大小限制" in (records["large.py"].error or "")
    assert any(item.code == "file_too_large" for item in index.diagnostics)
    assert all(symbol.file_path != "large.py" for symbol in index.symbols)


def test_scanner_records_syntax_error_without_crashing(tmp_path: Path) -> None:
    """一个语法错误文件不能阻止其他文件建立索引。"""
    workspace, root = _workspace(tmp_path)
    _write(root, "good.py", "def good():\n    return 1\n")
    _write(root, "bad.py", "def broken(:\n    pass\n")

    index = scan_project(workspace)
    records = {record.path: record for record in index.files}
    assert records["good.py"].parse_status == "ok"
    assert records["bad.py"].parse_status == "error"
    assert any(item.code == "syntax_error" for item in index.diagnostics)
    assert any(symbol.name == "good" for symbol in index.symbols)
    assert not any(symbol.file_path == "bad.py" for symbol in index.symbols)


def test_scanner_skips_symlink_files(tmp_path: Path) -> None:
    """Workspace 外部的符号链接不能通过别名绕过路径边界。"""
    workspace, root = _workspace(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("def secret():\n    pass\n", encoding="utf-8")
    link = root / "link.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不支持符号链接测试")

    index = scan_project(workspace)
    records = {record.path: record for record in index.files}
    assert records["link.py"].parse_status == "skipped"
    assert any(item.code == "symlink_escape" for item in index.diagnostics)
    assert not any(symbol.file_path == "link.py" for symbol in index.symbols)


def test_scanner_is_deterministic(tmp_path: Path) -> None:
    """同一目录多次扫描应得到相同的文件、符号和边顺序。"""
    workspace, root = _workspace(tmp_path)
    _write(root, "b.py", "def b():\n    pass\n")
    _write(root, "a.py", "class A:\n    def run(self):\n        b()\n")

    first = scan_project(workspace)
    second = scan_project(workspace)
    assert first.files == second.files
    assert first.symbols == second.symbols
    assert first.imports == second.imports
    assert first.calls == second.calls


class FakeAdapter(LanguageAdapter):
    """用于验证扫描器语言无关扩展点。"""

    language = "fake"
    extensions = (".fake",)

    def parse(self, source):
        return ParsedFile(
            symbols=[
                SymbolRecord(
                    id=f"module:{source.relative_path}",
                    kind="module",
                    name="fake",
                    qualified_name="fake",
                    file_path=source.relative_path,
                    start_line=1,
                    end_line=1,
                    language=self.language,
                )
            ]
        )


def test_scanner_accepts_additional_language_adapters(tmp_path: Path) -> None:
    """未来语言只需新增适配器，不需要修改扫描器主体。"""
    workspace, root = _workspace(tmp_path)
    _write(root, "main.py", "x = 1\n")
    _write(root, "config.fake", "anything\n")

    scanner = ProjectScanner(
        workspace,
        adapters=[PythonAstParser(), FakeAdapter()],
    )
    index = scanner.scan()
    assert {record.language for record in index.files} == {"python", "fake"}
    assert any(symbol.language == "fake" for symbol in index.symbols)
