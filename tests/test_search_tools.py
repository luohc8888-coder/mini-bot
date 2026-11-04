"""搜索工具的路径边界、glob 语义与目录剪枝回归。"""

import os
from pathlib import Path

import pytest

from mini_harness.tools import FindFilesTool, GrepTool, ListDirTool
from mini_harness.workspace import WorkspaceResolver


@pytest.mark.parametrize("pattern", ["../*.py", "sub/../../*.py", "/tmp/*.py", "a**b"])
@pytest.mark.asyncio
async def test_find_rejects_escaping_or_invalid_patterns(tmp_path: Path, pattern: str) -> None:
    tool = FindFilesTool(WorkspaceResolver().resolve(tmp_path))
    result = await tool.execute(pattern)
    assert result.is_error and "invalid glob" in result.content


@pytest.mark.asyncio
async def test_find_does_not_follow_external_links_or_directory_cycles(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "secret.py").write_text("private")
    (root / "external").symlink_to(tmp_path, target_is_directory=True)
    (root / "secret-link.py").symlink_to(tmp_path / "secret.py")
    (root / "cycle").symlink_to(root, target_is_directory=True)
    (root / "self-link").symlink_to(root / "self-link")
    (root / "real.py").write_text("public")
    (root / "alias.py").symlink_to(root / "real.py")
    tool = FindFilesTool(WorkspaceResolver().resolve(root))
    result = await tool.execute("*.py")
    assert result.success
    assert set(result.content.splitlines()) == {"real.py", "alias.py"}
    external = await tool.execute("external/*.py")
    assert external.success and "No matches" in external.content


@pytest.mark.parametrize("kind", ["file", "dir", "any"])
@pytest.mark.parametrize("pattern", ["*.py", "**/*.py", "src/*.py", "src/**/*.py", "[mr]*.py", "src/"])
@pytest.mark.asyncio
async def test_glob_results_match_pathlib_for_regular_tree(tmp_path: Path, pattern: str, kind: str) -> None:
    for relative in ("main.py", "src/main.py", "src/deep/mod.py", "nested/src/runner.py", "notes.txt"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("text")
    expected = {
        str(item.relative_to(tmp_path)) + ("/" if item.is_dir() else "")
        for item in tmp_path.rglob(pattern)
        if kind == "any" or (kind == "file" and item.is_file()) or (kind == "dir" and item.is_dir())
    }
    result = await FindFilesTool(WorkspaceResolver().resolve(tmp_path)).execute(pattern, kind=kind)
    assert result.success
    actual = set() if result.content.startswith("No matches") else set(result.content.splitlines())
    assert actual == expected


@pytest.mark.parametrize("tool_name", ["list_dir", "find_files", "grep"])
@pytest.mark.asyncio
async def test_noise_directories_are_pruned_before_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str) -> None:
    for name in ("node_modules", ".git", ".venv"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "hidden.py").write_text("match")
    (tmp_path / "main.py").write_text("match")
    def guard(original):
        def guarded(path):
            if not isinstance(path, int):
                assert Path(path).name not in {"node_modules", ".git", ".venv"}, "entered ignored directory"
            return original(path)
        return guarded

    monkeypatch.setattr(os, "scandir", guard(os.scandir))
    monkeypatch.setattr(os, "listdir", guard(os.listdir))
    workspace = WorkspaceResolver().resolve(tmp_path)
    if tool_name == "list_dir":
        result = await ListDirTool(workspace).execute(".", recursive=True)
    elif tool_name == "find_files":
        result = await FindFilesTool(workspace).execute("**/*.py")
    else:
        result = await GrepTool(workspace).execute("match")
    assert result.success and "main.py" in result.content
    assert "hidden.py" not in result.content


def test_legacy_module_imports_remain_available() -> None:
    from mini_harness.tools.filesystem import FindFilesTool as LegacyFind
    from mini_harness.tools.patch import ApplyPatchTool as LegacyPatch
    from mini_harness.tools import ApplyPatchTool
    assert LegacyFind is FindFilesTool
    assert LegacyPatch is ApplyPatchTool
