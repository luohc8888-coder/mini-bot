"""Workspace 文件读写工具测试。"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from mini_harness.agent import AgentRunSpec, AgentRunner
from mini_harness.cli import InteractiveCLI, ProfileStore
from mini_harness.providers import LLMResponse, MockProvider, ToolCallRequest
from mini_harness.tools import (
    ApplyPatchTool,
    EditFileTool,
    FileInfoTool,
    FindFilesTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    ToolRegistry,
    WriteFileTool,
)
from mini_harness.workspace import WorkspaceResolver


def workspace(base: Path):
    """创建并返回绑定到临时目录的 Workspace。"""
    base.mkdir(parents=True, exist_ok=True)
    return WorkspaceResolver(cwd=base).resolve(base)


@pytest.mark.asyncio
async def test_read_file_returns_numbered_text(tmp_path: Path) -> None:
    """读取 Markdown 时返回带行号内容和文件尾提示。"""
    ws = workspace(tmp_path)
    target = tmp_path / "docs" / "note.md"
    target.parent.mkdir()
    target.write_text("# Title\n\nHello", encoding="utf-8")

    result = await ReadFileTool(ws).execute("docs/note.md")

    assert result.success
    assert "1| # Title" in result.content
    assert "2| " in result.content
    assert "3| Hello" in result.content
    assert "(End of file — 3 lines total)" in result.content


@pytest.mark.asyncio
async def test_read_file_offset_and_limit(tmp_path: Path) -> None:
    """offset/limit 只返回指定行范围并提示下一页。"""
    ws = workspace(tmp_path)
    target = tmp_path / "note.md"
    target.write_text("\n".join(f"line{i}" for i in range(1, 8)), encoding="utf-8")

    result = await ReadFileTool(ws).execute("note.md", offset=3, limit=2)

    assert result.success
    assert "3| line3" in result.content
    assert "4| line4" in result.content
    assert "line2" not in result.content
    assert "line5" not in result.content
    assert "Use offset=5 to continue." in result.content


@pytest.mark.asyncio
async def test_read_file_missing_and_binary_errors(tmp_path: Path) -> None:
    """缺失文件和非 UTF-8 文件返回 ToolResult.error。"""
    ws = workspace(tmp_path)
    missing = await ReadFileTool(ws).execute("missing.md")
    assert missing.is_error
    assert "File not found" in missing.content

    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00")
    binary = await ReadFileTool(ws).execute("bad.md")
    assert binary.is_error
    assert "UTF-8" in binary.content


@pytest.mark.asyncio
async def test_read_file_rejects_workspace_escape(tmp_path: Path) -> None:
    """读取路径不允许逃出 Workspace。"""
    ws = workspace(tmp_path / "workspace")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")

    result = await ReadFileTool(ws).execute("../outside.md")

    assert result.is_error
    assert "不在 Workspace 内" in result.content


@pytest.mark.asyncio
async def test_list_dir_recursive_skips_noise_directories(tmp_path: Path) -> None:
    """list_dir 递归发现文件，同时忽略 .git/__pycache__ 等目录。"""
    ws = workspace(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"x")

    result = await ListDirTool(ws).execute(".", recursive=True)

    assert result.success
    assert "README.md" in result.content
    assert "src/" in result.content
    assert "src/main.py" in result.content
    assert ".git" not in result.content
    assert "__pycache__" not in result.content


@pytest.mark.asyncio
async def test_list_dir_rejects_workspace_escape(tmp_path: Path) -> None:
    """list_dir 也不能列出 Workspace 外目录。"""
    ws = workspace(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    result = await ListDirTool(ws).execute("../outside")

    assert result.is_error
    assert "不在 Workspace 内" in result.content


@pytest.mark.asyncio
async def test_find_files_filters_glob_noise_and_kind(tmp_path: Path) -> None:
    """find_files 按 glob 查找文件，过滤噪声目录并支持目录/数量限制。"""
    ws = workspace(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "src" / "util.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("# Demo", encoding="utf-8")
    (tmp_path / "nested" / "deep").mkdir(parents=True)
    (tmp_path / "nested" / "deep" / "mod.py").write_text("y = 2", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("secret", encoding="utf-8")

    files = await FindFilesTool(ws).execute("**/*.py")

    assert files.success
    assert "src/main.py" in files.content
    assert "src/util.py" in files.content
    assert "nested/deep/mod.py" in files.content
    assert "docs/readme.md" not in files.content
    assert ".git" not in files.content

    directories = await FindFilesTool(ws).execute("*", kind="dir")
    assert "src/" in directories.content
    assert "nested/deep/" in directories.content
    assert "src/main.py" not in directories.content

    limited = await FindFilesTool(ws).execute("**/*.py", max_results=1)
    assert "(truncated, showing first 1 of 3 entries)" in limited.content

    scoped = await FindFilesTool(ws).execute("*.py", path="src")
    assert "main.py" in scoped.content
    assert "nested" not in scoped.content


@pytest.mark.asyncio
async def test_find_files_rejects_invalid_arguments_and_escape(tmp_path: Path) -> None:
    """空 pattern、非法 kind、文件起点和越界路径都返回错误。"""
    ws = workspace(tmp_path / "workspace")
    (tmp_path / "outside").mkdir()
    tool = FindFilesTool(ws)

    assert (await tool.execute("")).is_error
    assert (await tool.execute("*", kind="invalid")).is_error
    assert (await tool.execute("*", max_results=0)).is_error

    file_target = tmp_path / "workspace" / "note.md"
    file_target.write_text("note", encoding="utf-8")
    not_directory = await tool.execute("*", path="note.md")
    assert not_directory.is_error
    assert "Not a directory" in not_directory.content

    escape = await tool.execute("*", path="../outside")
    assert escape.is_error
    assert "不在 Workspace 内" in escape.content


@pytest.mark.asyncio
async def test_grep_finds_matching_lines_and_skips_noise(tmp_path: Path) -> None:
    """grep 返回文件路径和行号，并跳过 .git 与二进制文件。"""
    ws = workspace(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "runner.py").write_text(
        "class AgentRunner:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        "AgentRunner is documented here", encoding="utf-8"
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        "class AgentRunner", encoding="utf-8"
    )
    (tmp_path / "bad.bin").write_bytes(b"\xff\xfe")

    result = await GrepTool(ws).execute("class AgentRunner", glob="*.py")

    assert result.success
    assert "src/runner.py:1:" in result.content
    assert "README.md" not in result.content
    assert ".git" not in result.content


@pytest.mark.asyncio
async def test_grep_rejects_workspace_escape(tmp_path: Path) -> None:
    """grep 不能搜索 Workspace 外路径。"""
    ws = workspace(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret = 1", encoding="utf-8")

    result = await GrepTool(ws).execute("secret", path="../outside")

    assert result.is_error
    assert "不在 Workspace 内" in result.content


@pytest.mark.asyncio
async def test_write_file_creates_markdown_and_parent_directories(
    tmp_path: Path,
) -> None:
    """write_file 可以创建新的 Markdown 文件并自动建父目录。"""
    ws = workspace(tmp_path)
    result = await WriteFileTool(ws).execute(
        "notes/new.md",
        "# New\n\ncontent",
    )

    assert result.success
    assert "Successfully wrote" in result.content
    assert (tmp_path / "notes" / "new.md").read_text(encoding="utf-8") == (
        "# New\n\ncontent"
    )


@pytest.mark.asyncio
async def test_write_file_overwrites_existing_file(tmp_path: Path) -> None:
    """参考 Nanobot 语义，write_file 完整覆盖已有文件。"""
    ws = workspace(tmp_path)
    target = tmp_path / "note.md"
    target.write_text("old", encoding="utf-8")

    result = await WriteFileTool(ws).execute("note.md", "new")

    assert result.success
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_write_file_rejects_workspace_escape(tmp_path: Path) -> None:
    """写入路径不允许逃出 Workspace。"""
    ws = workspace(tmp_path / "workspace")
    outside = tmp_path / "outside.md"

    result = await WriteFileTool(ws).execute("../outside.md", "not allowed")

    assert result.is_error
    assert not outside.exists()


@pytest.mark.asyncio
async def test_agent_runner_can_call_read_file(tmp_path: Path) -> None:
    """模型通过 ToolRegistry 调用 read_file 时，工具结果进入消息历史。"""
    ws = workspace(tmp_path)
    (tmp_path / "note.md").write_text("# Title\n\nHello", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ReadFileTool(ws))
    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="read_001",
                        name="read_file",
                        arguments={"path": "note.md"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="读取完成", finish_reason="stop"),
        ]
    )

    result = await AgentRunner(provider, registry).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "读取 note.md"}]
        )
    )

    assert result.final_content == "读取完成"
    assert result.messages[1]["tool_calls"][0]["name"] == "read_file"
    assert result.messages[2]["role"] == "tool"
    assert "1| # Title" in result.messages[2]["content"]


def test_file_tools_expose_nanobot_style_schemas(tmp_path: Path) -> None:
    """文件工具可通过 ToolRegistry 注册并暴露正确参数定义。"""
    registry = ToolRegistry()
    ws = workspace(tmp_path)
    registry.register(FindFilesTool(ws))
    registry.register(GrepTool(ws))
    registry.register(ListDirTool(ws))
    registry.register(ReadFileTool(ws))
    registry.register(WriteFileTool(ws))
    registry.register(EditFileTool(ws))
    registry.register(ApplyPatchTool(ws))
    registry.register(FileInfoTool(ws))

    definitions = {item["function"]["name"]: item for item in registry.get_definitions()}
    assert set(definitions) == {
        "apply_patch",
        "edit_file",
        "file_info",
        "find_files",
        "grep",
        "list_dir",
        "read_file",
        "write_file",
    }
    assert definitions["find_files"]["function"]["parameters"]["required"] == [
        "pattern"
    ]
    assert definitions["grep"]["function"]["parameters"]["required"] == ["pattern"]
    assert definitions["list_dir"]["function"]["parameters"]["required"] == ["path"]
    assert definitions["read_file"]["function"]["parameters"]["required"] == ["path"]
    assert definitions["write_file"]["function"]["parameters"]["required"] == [
        "path",
        "content",
    ]
    assert definitions["edit_file"]["function"]["parameters"]["required"] == [
        "path",
        "old_text",
        "new_text",
    ]
    assert definitions["apply_patch"]["function"]["parameters"]["required"] == [
        "edits"
    ]


def test_cli_registers_file_tools_when_workspace_is_available(tmp_path: Path) -> None:
    """正式 CLI 始终绑定 Workspace 并注册完整文件工具。"""
    store = ProfileStore(tmp_path / "profiles.json")
    cli = InteractiveCLI(data_root=tmp_path / "data", profile_store=store, workspace=tmp_path)
    assert "find_files" in cli.tools
    assert "grep" in cli.tools
    assert "list_dir" in cli.tools
    assert "read_file" in cli.tools
    assert "write_file" in cli.tools
    assert "edit_file" in cli.tools
    assert "apply_patch" in cli.tools
    assert "run_command" in cli.tools
    assert "file_info" in cli.tools


@pytest.mark.asyncio
async def test_append_preserves_original_bytes_and_creates_files(tmp_path: Path) -> None:
    tool = WriteFileTool(workspace(tmp_path))
    target = tmp_path / "note.md"
    target.write_bytes(b"original\r\n")
    assert (await tool.execute("note.md", "追加\n", mode="append")).success
    assert target.read_bytes() == b"original\r\n" + "追加\n".encode()
    assert (await tool.execute("nested/new.txt", "new", mode="append")).success
    assert (tmp_path / "nested/new.txt").read_text() == "new"
    assert (await tool.execute("note.md", "bad", mode="invalid")).is_error
    assert (await tool.execute("../outside.txt", "bad", mode="append")).is_error
    (tmp_path / "outside-link").symlink_to(tmp_path.parent / "outside.txt")
    assert (await tool.execute("outside-link", "bad", mode="append")).is_error


@pytest.mark.asyncio
async def test_file_info_reports_text_binary_directory_and_large_file(tmp_path: Path) -> None:
    tool = FileInfoTool(workspace(tmp_path))
    (tmp_path / "text.txt").write_text("你好\r\nsecond")
    data = json.loads((await tool.execute("text.txt")).content)
    assert data["type"] == "file" and data["line_count"] == 2
    assert data["size_bytes"] == len("你好\r\nsecond".encode())
    assert data["is_binary"] is False and data["sampled"] is False
    assert data["mtime"].endswith("+00:00")
    (tmp_path / "empty").touch()
    assert json.loads((await tool.execute("empty")).content)["line_count"] == 0
    for binary in (b"\x00abc", b"\xff\xfe"):
        (tmp_path / "binary").write_bytes(binary)
        info = json.loads((await tool.execute("binary")).content)
        assert info["is_binary"] is True and info["line_count"] is None
    assert json.loads((await tool.execute(".")).content)["type"] == "directory"
    (tmp_path / "large").write_bytes("中".encode() * 800000)
    info = json.loads((await tool.execute("large")).content)
    assert info["sampled"] is True and info["line_count"] is None
    assert info["is_binary"] is False  # 采样边缘不能把半个 UTF-8 字符判为二进制。
    assert (await tool.execute("missing")).is_error
    assert (await tool.execute("..")).is_error
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    assert (await tool.execute("link")).is_error


@pytest.mark.asyncio
async def test_grep_skips_symlinks_outside_workspace(tmp_path: Path) -> None:
    base = tmp_path / "workspace"
    ws = workspace(base)
    (tmp_path / "secret.txt").write_text("SECRET")
    (base / "link.txt").symlink_to(tmp_path / "secret.txt")
    result = await GrepTool(ws).execute("SECRET")
    assert result.success and "SECRET" not in result.content.replace("'SECRET'", "")


@pytest.mark.asyncio
async def test_edit_file_replaces_unique_text(tmp_path: Path) -> None:
    """edit_file 替换唯一匹配并保留文件内容。"""
    ws = workspace(tmp_path)
    target = tmp_path / "note.md"
    target.write_text("hello world\n", encoding="utf-8")

    result = await EditFileTool(ws).execute(
        "note.md",
        old_text="world",
        new_text="Mini Harness",
    )

    assert result.success
    assert "replaced 1 occurrence" in result.content
    assert target.read_text(encoding="utf-8") == "hello Mini Harness\n"


@pytest.mark.asyncio
async def test_edit_file_rejects_ambiguous_match_unless_replace_all(
    tmp_path: Path,
) -> None:
    """默认拒绝多处匹配，replace_all=True 时全部替换。"""
    ws = workspace(tmp_path)
    target = tmp_path / "note.md"
    target.write_text("foo foo", encoding="utf-8")

    ambiguous = await EditFileTool(ws).execute(
        "note.md",
        old_text="foo",
        new_text="bar",
    )
    assert ambiguous.is_error
    assert "appears 2 times" in ambiguous.content

    result = await EditFileTool(ws).execute(
        "note.md",
        old_text="foo",
        new_text="bar",
        replace_all=True,
    )
    assert result.success
    assert "replaced 2 occurrence" in result.content
    assert target.read_text(encoding="utf-8") == "bar bar"


@pytest.mark.asyncio
async def test_edit_file_preserves_crlf(tmp_path: Path) -> None:
    """编辑 CRLF 文件时保留原有换行风格。"""
    ws = workspace(tmp_path)
    target = tmp_path / "note.txt"
    target.write_bytes(b"a\r\nb\r\n")

    result = await EditFileTool(ws).execute(
        "note.txt",
        old_text="b",
        new_text="B",
    )

    assert result.success
    assert target.read_bytes() == b"a\r\nB\r\n"


@pytest.mark.asyncio
async def test_edit_file_errors_for_missing_text_and_escape(tmp_path: Path) -> None:
    """old_text 不存在、文件不存在和越界路径都返回错误。"""
    ws = workspace(tmp_path / "workspace")
    target = tmp_path / "workspace" / "note.md"
    target.parent.mkdir(exist_ok=True)
    target.write_text("hello", encoding="utf-8")

    missing_text = await EditFileTool(ws).execute(
        "note.md",
        old_text="not-there",
        new_text="x",
    )
    assert missing_text.is_error
    assert "old_text not found" in missing_text.content

    missing_file = await EditFileTool(ws).execute(
        "missing.md",
        old_text="x",
        new_text="y",
    )
    assert missing_file.is_error
    assert "File not found" in missing_file.content

    escape = await EditFileTool(ws).execute(
        "../outside.md",
        old_text="x",
        new_text="y",
    )
    assert escape.is_error
    assert "不在 Workspace 内" in escape.content


@pytest.mark.asyncio
async def test_agent_runner_can_call_edit_file(tmp_path: Path) -> None:
    """AgentRunner 能通过 ToolRegistry 执行 edit_file 并记录 tool 消息。"""
    ws = workspace(tmp_path)
    target = tmp_path / "note.md"
    target.write_text("hello old world", encoding="utf-8")
    tools = ToolRegistry()
    tools.register(EditFileTool(ws))
    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="edit_001",
                        name="edit_file",
                        arguments={
                            "path": "note.md",
                            "old_text": "old",
                            "new_text": "new",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="编辑完成", finish_reason="stop"),
        ]
    )

    result = await AgentRunner(provider, tools).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "编辑 note.md"}]
        )
    )

    assert result.final_content == "编辑完成"
    assert result.messages[1]["tool_calls"][0]["name"] == "edit_file"
    assert result.messages[2]["role"] == "tool"
    assert "Successfully edited" in result.messages[2]["content"]
    assert target.read_text(encoding="utf-8") == "hello new world"
