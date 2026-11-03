"""Workspace 文件读写工具测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.agent import AgentRunSpec, AgentRunner
from mini_harness.cli import InteractiveCLI, ProfileStore
from mini_harness.providers import LLMResponse, MockProvider, ToolCallRequest
from mini_harness.tools import (
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
    """四个文件工具可通过 ToolRegistry 注册并暴露正确参数定义。"""
    registry = ToolRegistry()
    ws = workspace(tmp_path)
    registry.register(GrepTool(ws))
    registry.register(ListDirTool(ws))
    registry.register(ReadFileTool(ws))
    registry.register(WriteFileTool(ws))

    definitions = {item["function"]["name"]: item for item in registry.get_definitions()}
    assert set(definitions) == {"grep", "list_dir", "read_file", "write_file"}
    assert definitions["grep"]["function"]["parameters"]["required"] == ["pattern"]
    assert definitions["list_dir"]["function"]["parameters"]["required"] == ["path"]
    assert definitions["read_file"]["function"]["parameters"]["required"] == ["path"]
    assert definitions["write_file"]["function"]["parameters"]["required"] == [
        "path",
        "content",
    ]


def test_cli_registers_file_tools_when_workspace_is_available(tmp_path: Path) -> None:
    """V1 CLI 在有 Workspace 时注册文件工具，V0 兼容模式不注册。"""
    store = ProfileStore(tmp_path / "profiles.json")
    cli = InteractiveCLI(profile_store=store, workspace=tmp_path)
    assert "grep" in cli.tools
    assert "list_dir" in cli.tools
    assert "read_file" in cli.tools
    assert "write_file" in cli.tools

    v0_cli = InteractiveCLI(profile_store=store)
    assert "grep" not in v0_cli.tools
    assert "list_dir" not in v0_cli.tools
    assert "read_file" not in v0_cli.tools
    assert "write_file" not in v0_cli.tools
