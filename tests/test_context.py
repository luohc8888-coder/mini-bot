"""ContextBuilder、默认 System Prompt 与 AGENTS.md 加载测试（V2）。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from mini_harness.agent import (
    ContextBuilder,
    ContextConfig,
    build_system_prompt,
    estimate_tokens,
)
from mini_harness.prompts.defaults import (
    DEFAULT_SYSTEM_PROMPT,
    build_default_system_prompt,
)


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def test_default_system_prompt_is_centralized_template() -> None:
    """默认提示词集中维护，并可通过统一函数生成。"""
    assert "You are Mini Harness" in DEFAULT_SYSTEM_PROMPT
    assert "不要编造工具执行结果" in DEFAULT_SYSTEM_PROMPT
    prompt = build_default_system_prompt(Path("/tmp/demo"))
    assert "/tmp/demo" in prompt
    assert "工作目录：." in build_default_system_prompt()


def test_build_messages_starts_with_system_prompt(tmp_path: Path) -> None:
    """构建后的第一条消息必须是 system，且只出现一次。"""
    workspace = make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[{"role": "user", "content": "旧问题"}],
        current_message={"role": "user", "content": "新问题"},
    )

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == build_system_prompt(workspace)
    assert sum(1 for item in messages if item["role"] == "system") == 1
    assert messages[-1] == {"role": "user", "content": "新问题"}


def test_agents_md_is_loaded_into_system_prompt(tmp_path: Path) -> None:
    """Workspace 根目录 AGENTS.md 的内容进入 System Prompt。"""
    workspace = make_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text(
        "# Project Instructions\n\n- 使用 Python 3.11+\n- 修改代码后运行 pytest\n",
        encoding="utf-8",
    )
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "AGENTS.md" in prompt
    assert "使用 Python 3.11+" in prompt
    assert "修改代码后运行 pytest" in prompt


def test_agents_md_missing_falls_back_to_default_prompt(tmp_path: Path) -> None:
    """AGENTS.md 不存在时正常生成默认 System Prompt。"""
    workspace = make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    assert builder.build_system_prompt() == build_default_system_prompt(
        workspace.resolve()
    )
    stats = builder.inspect([])
    assert stats.agents_md_status == "missing"


def test_agents_md_empty_is_ignored(tmp_path: Path) -> None:
    """空白 AGENTS.md 不应污染 System Prompt。"""
    workspace = make_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("   \n\n\t", encoding="utf-8")
    builder = ContextBuilder(workspace)

    assert builder.build_system_prompt() == build_default_system_prompt(
        workspace.resolve()
    )
    assert builder.inspect([]).agents_md_status == "empty"


def test_agents_md_update_is_picked_up_next_build(tmp_path: Path) -> None:
    """文件修改后，下一次 build_system_prompt 能看到新内容。"""
    workspace = make_workspace(tmp_path)
    agents = workspace / "AGENTS.md"
    agents.write_text("旧规则：使用 Python 3.10", encoding="utf-8")
    builder = ContextBuilder(workspace)
    assert "旧规则" in builder.build_system_prompt()

    agents.write_text("新规则：使用 Python 3.12", encoding="utf-8")

    prompt = builder.build_system_prompt()
    assert "新规则：使用 Python 3.12" in prompt
    assert "旧规则" not in prompt


def test_agents_md_can_be_disabled(tmp_path: Path) -> None:
    """include_agents_md=False 时不读取文件。"""
    workspace = make_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("不应出现", encoding="utf-8")
    builder = ContextBuilder(
        workspace,
        ContextConfig(include_agents_md=False),
    )

    assert "不应出现" not in builder.build_system_prompt()
    assert builder.inspect([]).agents_md_status == "disabled"


def test_agents_md_size_limit_skips_whole_file(tmp_path: Path) -> None:
    """超过大小限制时整份跳过，而不是截断成半份规则。"""
    workspace = make_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("x" * 200, encoding="utf-8")
    builder = ContextBuilder(
        workspace,
        ContextConfig(agents_md_max_bytes=64),
    )

    prompt = builder.build_system_prompt()
    assert "x" * 200 not in prompt
    assert builder.inspect([]).agents_md_status == "too_large"


def test_agents_md_uses_utf8_and_ignores_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """UTF-8 中文可读取；定位固定在构建时的 Workspace，不跟随 cwd。"""
    workspace = make_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text(
        "# 项目规则\n\n- 所有注释使用中文\n",
        encoding="utf-8",
    )
    other = tmp_path / "other"
    other.mkdir()
    (other / "AGENTS.md").write_text("来自错误目录的规则", encoding="utf-8")
    builder = ContextBuilder(workspace)
    monkeypatch.chdir(other)

    prompt = builder.build_system_prompt()

    assert "所有注释使用中文" in prompt
    assert "来自错误目录的规则" not in prompt


def test_build_messages_preserves_history_and_order(tmp_path: Path) -> None:
    """输出顺序为 system → selected_history → current；且不修改输入 history。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    history = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "第二轮"},
        {"role": "assistant", "content": "第二轮回答"},
    ]
    original = deepcopy(history)

    messages = builder.build_messages(
        history=history,
        current_message={"role": "user", "content": "当前问题"},
    )

    assert [item["role"] for item in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert [item["content"] for item in messages[1:]] == [
        "第一轮",
        "第一轮回答",
        "第二轮",
        "第二轮回答",
        "当前问题",
    ]
    assert history == original
    # 返回的 selected history 是副本，修改它不能反向污染 Session 历史。
    messages[1]["content"] = "被修改"
    assert history[1] == {"role": "assistant", "content": "第一轮回答"}


def test_estimate_tokens_is_documented_approximation() -> None:
    """Token 估算对空串、ASCII 和中文给出可预测结果。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 9) == 3
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("中文内容比 ascii 更密") > 4
