"""最小 System Prompt 构造器测试。"""

from __future__ import annotations

from pathlib import Path

from mini_harness.agent import build_system_prompt


def test_system_prompt_contains_workspace_and_project_analysis_rules() -> None:
    """System Prompt 应带 Workspace 路径和关键工具使用规则。"""
    workspace = Path("/tmp/sample-project")

    prompt = build_system_prompt(workspace)

    assert str(workspace) in prompt
    assert "list_dir" in prompt
    assert "grep" in prompt
    assert "write_file" in prompt
    assert "3~8 个关键文件" in prompt


def test_system_prompt_defaults_to_current_directory_label() -> None:
    """未提供 Workspace 时使用可读的当前目录占位符。"""
    assert "工作目录：." in build_system_prompt()
