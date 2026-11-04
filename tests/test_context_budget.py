"""Context Token Budget、历史选择与 Tool Calling 完整性测试（V2）。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mini_harness.agent import (
    ContextBuilder,
    ContextBudgetExceeded,
    ContextConfig,
    estimate_tokens,
)


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def simple_turn(index: int, content_size: int = 200) -> list[dict[str, Any]]:
    """构造一轮无工具调用的 user/assistant 历史。"""
    return [
        {"role": "user", "content": f"问题 {index}: " + "a" * content_size},
        {"role": "assistant", "content": f"回答 {index}: " + "b" * content_size},
    ]


def tool_turn(
    index: int,
    call_id: str,
    *,
    padding: int = 10,
) -> list[dict[str, Any]]:
    """构造一轮完整工具调用历史：user → assistant(tool_calls) → tool → assistant。"""
    return [
        {"role": "user", "content": f"执行工具 {index}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "name": "add",
                    "arguments": {"a": index, "b": padding},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "add",
            "content": str(index + padding),
        },
        {"role": "assistant", "content": f"工具 {index} 完成"},
    ]


def assert_tool_protocol(messages: list[dict[str, Any]]) -> None:
    """断言消息序列没有孤立 tool 结果或缺失结果的 tool_calls。"""
    if messages:
        assert messages[0]["role"] == "user"
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "user":
            assert not pending, f"上一轮工具调用未闭合：{pending}"
            pending = set()
        elif role == "assistant":
            assert not pending, f"tool_calls 后缺少结果：{pending}"
            calls = message.get("tool_calls") or []
            pending = {str(call["id"]) for call in calls}
        elif role == "tool":
            tool_call_id = str(message.get("tool_call_id"))
            assert tool_call_id in pending, f"孤立 tool 结果：{tool_call_id}"
            pending.remove(tool_call_id)
        else:
            raise AssertionError(f"未知角色：{role}")
    assert not pending, f"tool_calls 后缺少结果：{pending}"


def test_context_config_budget_formula(tmp_path: Path) -> None:
    """输入预算 = max_context_tokens - reserve_output_tokens。"""
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=4096, reserve_output_tokens=512),
    )
    stats = builder.inspect([])

    assert stats.max_context_tokens == 4096
    assert stats.reserve_output_tokens == 512
    assert stats.available_input_tokens == 3584
    assert stats.history_budget_tokens == (
        stats.available_input_tokens
        - stats.system_prompt_tokens
        - stats.current_message_tokens
        - stats.tool_schema_tokens
        - stats.safety_margin_tokens
    )


def test_tool_schema_overhead_is_reserved(tmp_path: Path) -> None:
    """Tool Schema 不在 messages 中，但必须占用输入预算。"""
    workspace = make_workspace(tmp_path)
    definitions = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "写入一个文本文件，路径必须位于 Workspace 内。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        }
    ]
    without_tools = ContextBuilder(workspace)
    with_tools = ContextBuilder(workspace, tool_definitions=definitions)

    stats_without = without_tools.inspect([])
    stats_with = with_tools.inspect([])

    assert stats_with.tool_schema_tokens > 0
    assert stats_with.history_budget_tokens < stats_without.history_budget_tokens


def test_long_history_selects_only_recent_budget_turns(tmp_path: Path) -> None:
    """长历史只保留预算允许的最近完整轮次，且从 user 边界开始。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    history = [
        message
        for index in range(80)
        for message in simple_turn(index)
    ]
    original = deepcopy(history)

    built = builder.build_context(
        history,
        {"role": "user", "content": "最新问题"},
    )

    stats = built.stats
    assert 0 < stats.selected_history_messages < len(history)
    assert stats.dropped_history_messages == (
        len(history) - stats.selected_history_messages
    )
    assert stats.estimated_input_tokens <= stats.available_input_tokens
    assert stats.selected_history_messages % 2 == 0
    selected = built.messages[1:-1]
    assert selected[0]["role"] == "user"
    assert selected == history[-len(selected):]
    assert history == original


def test_selection_keeps_tool_call_pairs_intact(tmp_path: Path) -> None:
    """历史裁剪后每个 tool 结果都有配对调用，每个调用都有结果。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    history: list[dict[str, Any]] = []
    for index in range(30):
        history.extend(simple_turn(index, content_size=600))
    history.extend(tool_turn(100, "call_recent_001"))
    history.extend(tool_turn(101, "call_recent_002", padding=20))

    built = builder.build_context(
        history,
        {"role": "user", "content": "继续"},
    )

    selected = built.messages[1:-1]
    assert selected
    assert selected[0]["role"] == "user"
    assert_tool_protocol(selected)
    # 最近两轮工具调用必须被保留下来，否则说明按消息硬裁断破坏了配对。
    assert any(
        message.get("tool_call_id") == "call_recent_002" for message in selected
    )
    assert any(
        message.get("tool_call_id") == "call_recent_001" for message in selected
    )


def test_orphan_tool_message_is_never_selected(tmp_path: Path) -> None:
    """历史开头的孤立 tool 消息被丢弃，不能出现在请求里。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    history = [
        {
            "role": "tool",
            "tool_call_id": "orphan_001",
            "name": "add",
            "content": "99",
        },
        *tool_turn(1, "call_valid"),
    ]

    built = builder.build_context(
        history,
        {"role": "user", "content": "继续"},
    )

    selected = built.messages[1:-1]
    assert all(
        message.get("tool_call_id") != "orphan_001" for message in selected
    )
    assert [message["role"] for message in selected] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert_tool_protocol(selected)


def test_missing_tool_result_turn_is_dropped(tmp_path: Path) -> None:
    """最新一轮 assistant tool_calls 缺少结果时，整轮丢弃。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    history = [
        *tool_turn(1, "call_ok"),
        {"role": "user", "content": "坏掉的轮次"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_missing",
                    "name": "add",
                    "arguments": {"a": 1, "b": 2},
                }
            ],
        },
    ]

    built = builder.build_context(
        history,
        {"role": "user", "content": "继续"},
    )

    selected = built.messages[1:-1]
    assert selected == []
    assert built.stats.dropped_history_messages == len(history)


def test_history_budget_zero_still_sends_current_message(tmp_path: Path) -> None:
    """固定开销刚好占满预算时，仍可发送 system + 当前消息。"""
    workspace = make_workspace(tmp_path)
    from mini_harness.agent.context import estimate_message_tokens

    probe = ContextBuilder(workspace)
    current = {"role": "user", "content": "hi"}
    fixed = (
        probe.inspect([]).system_prompt_tokens
        + estimate_message_tokens(current)
    )

    builder = ContextBuilder(
        workspace,
        ContextConfig(
            max_context_tokens=fixed + 10,
            reserve_output_tokens=10,
        ),
    )
    built = builder.build_context(
        [*tool_turn(1, "call_old")],
        current,
    )

    assert built.stats.history_budget_tokens == 0
    assert [message["role"] for message in built.messages] == ["system", "user"]


def test_current_message_over_budget_raises_without_truncation(
    tmp_path: Path,
) -> None:
    """当前用户消息本身超预算时明确报错，绝不静默截断。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    current_text = "超" * 8000
    history = [{"role": "user", "content": "旧历史"}]

    with pytest.raises(ContextBudgetExceeded) as exc_info:
        builder.build_messages(
            history=history,
            current_message={"role": "user", "content": current_text},
        )

    error = exc_info.value
    assert error.estimated_tokens > error.available_input_tokens
    assert error.available_input_tokens == 7168
    assert error.breakdown["current_message"] > 7000
    # 历史没有被修改。
    assert history == [{"role": "user", "content": "旧历史"}]


def test_oversized_system_prompt_is_reported_as_budget_error(
    tmp_path: Path,
) -> None:
    """System Prompt 已超预算时返回清晰错误，而不是发送超预算请求。"""
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=300, reserve_output_tokens=50),
    )

    with pytest.raises(ContextBudgetExceeded) as exc_info:
        builder.build_messages([], {"role": "user", "content": "hi"})

    assert "System Prompt" in str(exc_info.value)
    assert exc_info.value.breakdown["system_prompt"] > 0


def test_stats_role_counts_and_drop_counts(tmp_path: Path) -> None:
    """统计中的角色分布、选中数和丢弃数来自真实构建结果。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    history = [message for index in range(30) for message in simple_turn(index)]

    stats = builder.build_context(
        history,
        {"role": "user", "content": "问题"},
    ).stats

    assert stats.session_role_counts == {
        "user": 30,
        "assistant": 30,
    }
    assert sum(stats.selected_history_role_counts.values()) == (
        stats.selected_history_messages
    )
    assert stats.selected_history_messages + stats.dropped_history_messages == 60


def test_estimate_tokens_counts_structure_overhead() -> None:
    """消息估算不仅计算 content，还计算角色、tool_calls 和结构开销。"""
    from mini_harness.agent.context import estimate_message_tokens

    plain = {"role": "user", "content": "hello"}
    tool_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_123",
                "name": "write_file",
                "arguments": {"path": "a.md", "content": "x" * 100},
            }
        ],
    }

    assert estimate_message_tokens(plain) > estimate_tokens("hello")
    assert estimate_message_tokens(tool_message) > estimate_message_tokens(plain)
