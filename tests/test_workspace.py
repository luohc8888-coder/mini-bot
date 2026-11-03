"""Workspace 路径解析与稳定 ID 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.workspace import (
    WorkspacePathError,
    WorkspaceResolver,
    workspace_id,
)


def test_workspace_id_is_stable_for_same_path(tmp_path: Path) -> None:
    """同一绝对路径在多次计算中必须得到相同 ID。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert workspace_id(ws) == workspace_id(ws.resolve())
    assert len(workspace_id(ws)) == 16


def test_workspace_id_differs_for_different_paths(tmp_path: Path) -> None:
    """不同目录必须得到不同 ID。"""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert workspace_id(first) != workspace_id(second)


def test_resolver_uses_explicit_directory_and_injected_cwd(tmp_path: Path) -> None:
    """显式路径优先；相对路径以 Resolver 的 cwd 为基准。"""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    nested = cwd / "project"
    nested.mkdir()

    resolver = WorkspaceResolver(cwd=cwd)
    assert resolver.resolve().path == cwd.resolve()
    assert resolver.resolve("project").path == nested.resolve()
    assert resolver.resolve(nested).path == nested.resolve()


def test_resolver_rejects_missing_or_file_path(tmp_path: Path) -> None:
    """不存在路径和普通文件不能作为 Workspace。"""
    from mini_harness.workspace import WorkspaceError

    resolver = WorkspaceResolver(cwd=tmp_path)
    with pytest.raises(WorkspaceError):
        resolver.resolve("missing")
    file_path = tmp_path / "file.txt"
    file_path.write_text("hello", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        resolver.resolve(file_path)


def test_workspace_path_policy_rejects_parent_and_absolute_escape(
    tmp_path: Path,
) -> None:
    """../ 和 Workspace 外的绝对路径都必须被拒绝。"""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    ws = WorkspaceResolver(cwd=tmp_path).resolve(workspace_dir)
    with pytest.raises(WorkspacePathError):
        ws.resolve_path("../outside/secret.txt")
    with pytest.raises(WorkspacePathError):
        ws.resolve_path(secret)
    # Workspace 内部的普通相对路径允许。
    assert ws.resolve_path("README.md") == workspace_dir / "README.md"


def test_workspace_path_policy_rejects_symlink_escape(tmp_path: Path) -> None:
    """已存在且指向外部的符号链接不能被当作 Workspace 内部文件。"""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace_dir / "outside_link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不支持符号链接测试")

    ws = WorkspaceResolver(cwd=tmp_path).resolve(workspace_dir)
    with pytest.raises(WorkspacePathError):
        ws.resolve_path("outside_link/secret.txt")
