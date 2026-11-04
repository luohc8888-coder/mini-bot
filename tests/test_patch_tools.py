"""apply_patch 批量精确编辑工具测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.tools import ApplyPatchTool, ToolRegistry
from mini_harness.workspace import WorkspaceResolver
import mini_harness.tools.edit as edit_module


def workspace(base: Path):
    """创建并返回绑定到临时目录的 Workspace。"""
    base.mkdir(parents=True, exist_ok=True)
    return WorkspaceResolver(cwd=base).resolve(base)


@pytest.mark.asyncio
async def test_apply_patch_edits_multiple_files_and_composes_same_file(
    tmp_path: Path,
) -> None:
    """跨文件编辑按顺序执行，同一文件的多个编辑依次叠加。"""
    ws = workspace(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("hello world\nfoo foo\n", encoding="utf-8")
    module = tmp_path / "module.py"
    module.write_text("old_name = 1\n", encoding="utf-8")

    result = await ApplyPatchTool(ws).execute(
        [
            {"path": "note.md", "old_text": "world", "new_text": "Mini Harness"},
            {
                "path": "note.md",
                "old_text": "foo",
                "new_text": "bar",
                "replace_all": True,
            },
            {"path": "module.py", "old_text": "old_name", "new_text": "new_name"},
        ]
    )

    assert result.success
    assert "applied 3 edit(s) across 2 file(s)" in result.content
    assert "replaced 4 occurrence(s)" in result.content
    assert note.read_text(encoding="utf-8") == "hello Mini Harness\nbar bar\n"
    assert module.read_text(encoding="utf-8") == "new_name = 1\n"


@pytest.mark.asyncio
async def test_apply_patch_preserves_crlf(tmp_path: Path) -> None:
    """编辑 CRLF 文件时保留原有换行风格。"""
    ws = workspace(tmp_path)
    target = tmp_path / "note.txt"
    target.write_bytes(b"a\r\nb\r\n")

    result = await ApplyPatchTool(ws).execute(
        [{"path": "note.txt", "old_text": "b", "new_text": "B"}]
    )

    assert result.success
    assert target.read_bytes() == b"a\r\nB\r\n"


@pytest.mark.asyncio
async def test_apply_patch_writes_nothing_when_any_edit_fails(tmp_path: Path) -> None:
    """任一编辑校验失败时，前面的合法编辑也不能落盘。"""
    ws = workspace(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("alpha", encoding="utf-8")
    second.write_text("beta", encoding="utf-8")

    result = await ApplyPatchTool(ws).execute(
        [
            {"path": "first.txt", "old_text": "alpha", "new_text": "ALPHA"},
            {"path": "second.txt", "old_text": "missing", "new_text": "BETA"},
        ]
    )

    assert result.is_error
    assert "edits[1]: old_text not found" in result.content
    assert first.read_text(encoding="utf-8") == "alpha"
    assert second.read_text(encoding="utf-8") == "beta"


@pytest.mark.asyncio
async def test_apply_patch_rejects_ambiguous_and_invalid_edits(
    tmp_path: Path,
) -> None:
    """多处匹配、空列表、无变化和非法字段都返回错误。"""
    ws = workspace(tmp_path)
    target = tmp_path / "note.md"
    target.write_text("foo foo", encoding="utf-8")
    tool = ApplyPatchTool(ws)

    ambiguous = await tool.execute(
        [{"path": "note.md", "old_text": "foo", "new_text": "bar"}]
    )
    assert ambiguous.is_error
    assert "appears 2 times" in ambiguous.content
    assert target.read_text(encoding="utf-8") == "foo foo"

    assert (await tool.execute([])).is_error
    assert (await tool.execute("bad")).is_error
    assert (
        await tool.execute(
            [{"path": "note.md", "old_text": "foo", "new_text": "foo"}]
        )
    ).is_error
    assert (
        await tool.execute(
            [{"path": "note.md", "old_text": "", "new_text": "x"}]
        )
    ).is_error


@pytest.mark.asyncio
async def test_apply_patch_reports_missing_file_binary_and_escape(
    tmp_path: Path,
) -> None:
    """缺失文件、非 UTF-8 文件和越界路径都返回 ToolResult.error。"""
    ws = workspace(tmp_path / "workspace")
    binary = tmp_path / "workspace" / "bad.bin"
    binary.write_bytes(b"\xff\xfe")
    tool = ApplyPatchTool(ws)

    missing = await tool.execute(
        [{"path": "missing.md", "old_text": "x", "new_text": "y"}]
    )
    assert missing.is_error
    assert "File not found" in missing.content

    bad = await tool.execute(
        [{"path": "bad.bin", "old_text": "x", "new_text": "y"}]
    )
    assert bad.is_error
    assert "UTF-8" in bad.content

    escape = await tool.execute(
        [{"path": "../outside.md", "old_text": "x", "new_text": "y"}]
    )
    assert escape.is_error
    assert "不在 Workspace 内" in escape.content


@pytest.mark.asyncio
async def test_registry_validates_nested_patch_arguments(tmp_path: Path) -> None:
    """Registry 能定位 edits 数组项的缺字段、类型错误和未知字段。"""
    ws = workspace(tmp_path)
    registry = ToolRegistry()
    registry.register(ApplyPatchTool(ws))

    missing = await registry.execute(
        "apply_patch",
        {"edits": [{"path": "note.md", "new_text": "x"}]},
    )
    assert missing.is_error
    assert "缺少必填参数：edits[0].old_text" in missing.content

    wrong_type = await registry.execute(
        "apply_patch",
        {
            "edits": [
                {"path": 1, "old_text": "x", "new_text": "y"}
            ]
        },
    )
    assert wrong_type.is_error
    assert "参数 edits[0].path 类型错误" in wrong_type.content

    unknown = await registry.execute(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "note.md",
                    "old_text": "x",
                    "new_text": "y",
                    "bad": 1,
                }
            ]
        },
    )
    assert unknown.is_error
    assert "不支持参数：edits[0].bad" in unknown.content

    empty = await registry.execute("apply_patch", {"edits": []})
    assert empty.is_error
    assert "至少需要 1 项" in empty.content


@pytest.mark.asyncio
async def test_write_failure_restores_previous_files_and_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = workspace(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"alpha\r\n")
    first.chmod(0o640)
    second.write_text("beta")
    real_write = edit_module._atomic_write_text

    def fail_second(target, content):
        if target == second:
            raise OSError("disk full")
        real_write(target, content)

    monkeypatch.setattr(edit_module, "_atomic_write_text", fail_second)
    result = await ApplyPatchTool(ws).execute([
        {"path": "first.txt", "old_text": "alpha", "new_text": "ALPHA"},
        {"path": "second.txt", "old_text": "beta", "new_text": "BETA"},
    ])
    assert result.is_error and "restored" in result.content
    assert "second.txt" in result.content
    assert first.read_bytes() == b"alpha\r\n" and second.read_text() == "beta"
    assert first.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_rollback_failure_identifies_files_still_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = workspace(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("alpha")
    second.write_text("beta")
    real_write = edit_module._atomic_write_text

    def fail_commit_and_rollback(target, content):
        if target == second or (target == first and content == "alpha"):
            raise OSError("simulated failure")
        real_write(target, content)

    monkeypatch.setattr(edit_module, "_atomic_write_text", fail_commit_and_rollback)
    result = await ApplyPatchTool(ws).execute([
        {"path": "first.txt", "old_text": "alpha", "new_text": "ALPHA"},
        {"path": "second.txt", "old_text": "beta", "new_text": "BETA"},
    ])
    assert result.is_error and "Rollback failed" in result.content
    assert "first.txt" in result.content
    assert first.read_text() == "ALPHA" and second.read_text() == "beta"


@pytest.mark.asyncio
async def test_invalid_utf8_replacement_is_rejected_before_any_write(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (tmp_path / "first.txt").write_text("alpha")
    (tmp_path / "second.txt").write_text("beta")
    result = await ApplyPatchTool(ws).execute([
        {"path": "first.txt", "old_text": "alpha", "new_text": "ALPHA"},
        {"path": "second.txt", "old_text": "beta", "new_text": "\ud800"},
    ])
    assert result.is_error
    assert (tmp_path / "first.txt").read_text() == "alpha"
    assert (tmp_path / "second.txt").read_text() == "beta"


@pytest.mark.asyncio
async def test_replacement_expansion_is_bounded_before_allocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = workspace(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("x" * 90)
    monkeypatch.setattr(edit_module, "_MAX_EDIT_BYTES", 100)
    result = await ApplyPatchTool(ws).execute([
        {"path": "note.txt", "old_text": "x", "new_text": "yy", "replace_all": True},
    ])
    assert result.is_error and "resulting file too large (180 bytes)" in result.content
    assert target.read_text() == "x" * 90


@pytest.mark.asyncio
async def test_no_net_change_does_not_rewrite_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = workspace(tmp_path)
    (tmp_path / "note.txt").write_text("alpha")

    def must_not_write(*args):
        pytest.fail("no net change should not write")

    monkeypatch.setattr(edit_module, "_atomic_write_text", must_not_write)
    result = await ApplyPatchTool(ws).execute([
        {"path": "note.txt", "old_text": "alpha", "new_text": "beta"},
        {"path": "note.txt", "old_text": "beta", "new_text": "alpha"},
    ])
    assert result.success and "no net file changes" in result.content
