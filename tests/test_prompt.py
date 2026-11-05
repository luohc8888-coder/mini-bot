"""最小 System Prompt 构造器测试。"""

from __future__ import annotations

from pathlib import Path

from mini_harness.prompts import build_default_system_prompt


def test_system_prompt_contains_workspace_and_project_analysis_rules() -> None:
    """System Prompt 应带 Workspace 路径和关键工具使用规则。"""
    workspace = Path("/tmp/sample-project")

    prompt = build_default_system_prompt(workspace)

    assert str(workspace) in prompt
    assert "list_dir" in prompt
    assert "find_files" in prompt
    assert "grep" in prompt
    assert "write_file" in prompt
    assert "edit_file" in prompt
    assert "apply_patch" in prompt
    assert "3~8 个关键文件" in prompt
    assert "只有用户要求保存报告" in prompt
    assert "run_command" in prompt
    assert "历史摘要中的 truncated" in prompt


def test_system_prompt_defaults_to_current_directory_label() -> None:
    """未提供 Workspace 时使用可读的当前目录占位符。"""
    assert "工作目录：." in build_default_system_prompt()
