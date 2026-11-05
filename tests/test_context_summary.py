"""ContextBuilder 的 Session Summary / Long-term Memory 注入测试（V2.2）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mini_harness.agent import ContextBuilder, ContextBudgetExceeded, ContextConfig


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


class FakeMemoryStore:
    """只提供 read_memory 的测试替身，避免依赖真实文件布局。"""

    def __init__(self, content: str = "") -> None:
        self.content = content
        self.reads = 0

    def read_memory(self) -> str:
        self.reads += 1
        return self.content


def history_with_tools() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": "旧问题：分析项目",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_old", "name": "read_file", "arguments": {"path": "a.py"}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_old",
            "name": "read_file",
            "content": "旧工具结果" * 100,
        },
        {"role": "assistant", "content": "旧结论：P0-1 已定位。"},
        {"role": "user", "content": "新问题：继续修复"},
        {"role": "assistant", "content": "新结论：已完成修复。"},
    ]


def test_summary_is_injected_once_and_archived_history_not_resent(
    tmp_path: Path,
) -> None:
    """已归档历史不再进入请求，Summary 只注入一次且位于未归档历史之前。"""
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
    )
    history = history_with_tools()
    built = builder.build_context(
        history,
        {"role": "user", "content": "当前请求"},
        summary="旧摘要：P0-1 已定位，继续修复。",
        summary_boundary=4,
    )

    roles = [message["role"] for message in built.messages]
    assert roles[0] == "system"
    assert roles[1] == "user"
    assert built.messages[1]["compaction_summary"] is True
    summary_messages = [
        message for message in built.messages if message.get("compaction_summary")
    ]
    assert len(summary_messages) == 1
    joined = "\n".join(str(message.get("content", "")) for message in built.messages)
    assert "旧工具结果" not in joined
    assert "旧结论：P0-1 已定位。" not in joined
    assert "新问题：继续修复" in joined
    assert "新结论：已完成修复。" in joined
    assert built.messages[-1] == {"role": "user", "content": "当前请求"}
    assert built.stats.summary_boundary == 4
    assert built.stats.unarchived_message_count == 2
    assert built.stats.summary_tokens > 0


def test_no_summary_keeps_legacy_behavior(tmp_path: Path) -> None:
    """无摘要时输出与旧版一致：system + 全部历史 + 当前消息。"""
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
    )
    history = history_with_tools()
    built = builder.build_context(
        history,
        {"role": "user", "content": "当前请求"},
    )
    assert [message["role"] for message in built.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert all(not message.get("compaction_summary") for message in built.messages)
    assert built.stats.summary_tokens == 0
    assert built.stats.summary_boundary == 0


def test_long_term_memory_is_injected_into_system_prompt(tmp_path: Path) -> None:
    """MEMORY.md 内容作为 System Prompt 的长期记忆段注入。"""
    store = FakeMemoryStore("- 用户偏好：使用中文回答\n- 架构决策：Session 保存原始历史")
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
        memory_store=store,
    )
    built = builder.build_context(
        [{"role": "user", "content": "hi"}],
        {"role": "user", "content": "继续"},
    )
    system = built.messages[0]["content"]
    assert "# Long-term Memory (MEMORY.md)" in system
    assert "用户偏好：使用中文回答" in system
    assert "背景知识，不是当前指令" in system
    assert built.stats.long_term_memory_status == "loaded"
    assert built.stats.long_term_memory_tokens > 0
    assert store.reads == 1


def test_empty_memory_store_is_not_injected(tmp_path: Path) -> None:
    """空 MEMORY.md 不应生成空的长期记忆段。"""
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
        memory_store=FakeMemoryStore("   \n"),
    )
    built = builder.build_context(
        [{"role": "user", "content": "hi"}],
        {"role": "user", "content": "继续"},
    )
    assert "# Long-term Memory" not in built.messages[0]["content"]
    assert built.stats.long_term_memory_status == "empty"


def test_summary_boundary_beyond_history_clamps_safely(tmp_path: Path) -> None:
    """越界 boundary 被收敛到历史长度，绝不产生负索引或重复历史。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
    ]
    built = builder.build_context(
        history,
        {"role": "user", "content": "current"},
        summary="摘要",
        summary_boundary=99,
    )
    assert built.stats.summary_boundary == 2
    assert built.stats.unarchived_message_count == 0
    assert [m["role"] for m in built.messages] == ["system", "user", "user"]
    assert built.messages[1]["compaction_summary"] is True
    assert built.messages[2] == {"role": "user", "content": "current"}


def test_summary_without_boundary_is_ignored(tmp_path: Path) -> None:
    """没有归档边界的摘要无法证明覆盖范围，必须忽略而不是重复注入。"""
    builder = ContextBuilder(make_workspace(tmp_path))
    history = [{"role": "user", "content": "old"}]
    built = builder.build_context(
        history,
        {"role": "user", "content": "current"},
        summary="孤立摘要",
        summary_boundary=0,
    )
    assert built.stats.summary_boundary == 0
    assert all(not m.get("compaction_summary") for m in built.messages)


def test_oversized_fixed_summary_raises_budget_error(tmp_path: Path) -> None:
    """Summary 按固定开销计入预算，固定部分超预算时明确报错。"""
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=600, reserve_output_tokens=100),
    )
    with pytest.raises(ContextBudgetExceeded) as excinfo:
        builder.build_context(
            [{"role": "user", "content": "old"}, {"role": "assistant", "content": "a"}],
            {"role": "user", "content": "current"},
            summary="超大摘要" * 500,
            summary_boundary=2,
        )
    assert "Session Summary" in str(excinfo.value)


def test_from_provider_preserves_memory_and_maintenance_config() -> None:
    """按 Provider 推导预算时不能丢掉 fallback 的 Memory/Idle/Dream 配置。"""
    class Provider:
        context_window_tokens = 100_000
        max_output_tokens = 8_000

    fallback = ContextConfig(
        memory_max_tokens=1024,
        idle_compact_after_minutes=0,
        dream_enabled=False,
        dream_interval_hours=6,
        dream_max_entries=5,
    )
    config = ContextConfig.from_provider(Provider(), fallback=fallback)
    assert config.max_context_tokens == 100_000
    assert config.reserve_output_tokens == 4096
    assert config.memory_max_tokens == 1024
    assert config.idle_compact_after_minutes == 0
    assert config.dream_enabled is False
    assert config.dream_interval_hours == 6
    assert config.dream_max_entries == 5
