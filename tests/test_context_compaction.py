"""V2.1 Context Compact 与 Provider 请求前预算治理回归测试。

覆盖用户要求的 8 组回归场景：
1. 最近一轮大工具输出 + 短 P0 结论仍能保留；
2. 同一回合连续工具调用时，每次 Provider 请求前都做预算检查；
3. Compact 前后 Tool Calling 协议合法；
4. Compact 摘要失败时安全回退、不破坏消息/Session；
5. Compact 仍超预算时不会无限循环；
6. 当前用户请求永远受保护；
7. Session 原始历史不被 Compact 破坏；
8. ``/last`` 反映最后一次真实 Provider 请求。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mini_harness.agent import (
    AgentLoop,
    AgentRunner,
    AgentRunSpec,
    ContextBudgetExceeded,
    ContextBuilder,
    ContextConfig,
    ContextCompactor,
    ContextGovernor,
    SUMMARY_SYSTEM_PROMPT,
)
from mini_harness.agent.compaction import split_message_regions, truncate_tool_results
from mini_harness.bus import InboundMessage, MessageBus
from mini_harness.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)
from mini_harness.providers.mock import MockProvider
from mini_harness.session import DEFAULT_SESSION_KEY, Session, SessionManager
from mini_harness.tools import AddTool, Tool, ToolRegistry
from mini_harness.tools.base import ToolResult


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def assert_tool_protocol(messages: list[dict[str, Any]]) -> None:
    """断言消息序列没有孤立 tool 结果或缺失结果的 tool_calls。"""
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
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


class BulkTool(Tool):
    """返回指定长度文本的测试工具，用于制造大型 Tool Result。"""

    name = "bulk"
    description = "返回大量测试文本"
    parameters = {
        "type": "object",
        "properties": {
            "size": {"type": "integer"},
            "label": {"type": "string"},
        },
        "required": ["size", "label"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, size: int, label: str) -> ToolResult:
        self.calls += 1
        return ToolResult(content=f"{label}:" + "x" * size)


class SummaryProvider(LLMProvider):
    """只接受摘要请求的 Provider；用于直接测试 ContextCompactor。"""

    def __init__(self, summary: str = "摘要：保留 P0-1 / P0-2。") -> None:
        self.summary = summary
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append(deepcopy(messages))
        assert messages[0]["content"] == SUMMARY_SYSTEM_PROMPT
        return LLMResponse(content=self.summary)


class FailingSummaryProvider(LLMProvider):
    """所有请求都失败；用于测试摘要失败回退。"""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        raise RuntimeError("summary provider down")


class ToolLoopProvider(LLMProvider):
    """按顺序请求多次工具，最后给出最终回答；摘要请求单独计数。"""

    def __init__(
        self,
        *,
        tool_calls: int,
        tool_size: int = 4000,
        summary: str = "摘要：用户要求分析并修复 P0 问题。",
    ) -> None:
        self.tool_calls = tool_calls
        self.tool_size = tool_size
        self.summary = summary
        self.requests: list[list[dict[str, Any]]] = []
        self.normal_requests: list[list[dict[str, Any]]] = []
        self.summary_calls = 0

    @staticmethod
    def _is_summary_request(messages: list[dict[str, Any]]) -> bool:
        return bool(
            messages
            and messages[0].get("content") == SUMMARY_SYSTEM_PROMPT
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.requests.append(deepcopy(messages))
        if self._is_summary_request(messages):
            self.summary_calls += 1
            return LLMResponse(content=self.summary)
        self.normal_requests.append(deepcopy(messages))
        # 使用真实 Provider 请求次数而非输入中的 tool 消息数推进脚本；
        # accepted H 压缩后旧 tool 消息可以被 Summary 替代。
        executed = len(self.normal_requests) - 1
        if executed < self.tool_calls:
            index = executed
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id=f"call_bulk_{index}",
                        name="bulk",
                        arguments={
                            "size": self.tool_size,
                            "label": f"tool{index}",
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="最终回答：P0 分析完成。")


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(BulkTool())
    return registry


def make_big_tool_turn(index: int, size: int = 8000) -> list[dict[str, Any]]:
    """构造一轮“用户 → 大量工具 → 最终短结论”的历史。"""
    return [
        {"role": "user", "content": f"分析项目中的 P0 问题（第 {index} 轮）"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_read_{index}",
                    "name": "read_file",
                    "arguments": {"path": "mini_harness/agent/context.py"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call_read_{index}",
            "name": "read_file",
            "content": "x" * size,
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_grep_{index}",
                    "name": "grep",
                    "arguments": {"pattern": "P0", "path": "mini_harness"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call_grep_{index}",
            "name": "grep",
            "content": "y" * size,
        },
        {"role": "assistant", "content": "旧轮次的一个简短结论。"},
    ]


# ---------------------------------------------------------------------------
# Test 1：最近一轮大工具输出 + 短 P0 结论
# ---------------------------------------------------------------------------


def test_latest_turn_conclusion_survives_large_tool_outputs(
    tmp_path: Path,
) -> None:
    """最新一轮整轮超预算时，P0 结论必须通过完成轮次摘要保留。"""
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=2000, reserve_output_tokens=100),
    )
    history = [
        *make_big_tool_turn(1, size=8000),
        {
            "role": "user",
            "content": "分析项目中的 P0 问题",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_p0_read",
                    "name": "read_file",
                    "arguments": {
                        "path": "mini_harness/agent/context.py"
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_p0_read",
            "name": "read_file",
            "content": "z" * 12000,
        },
        {
            "role": "assistant",
            "content": (
                "P0-1: _select_history 导致关键结论丢失。\n"
                "P0-2: Runner 工具循环未重新检查上下文预算。"
            ),
        },
    ]
    original = deepcopy(history)

    built = builder.build_context(
        history,
        {"role": "user", "content": "请修复刚才的 P0 问题。"},
    )

    all_contents = "\n".join(
        str(message.get("content", "")) for message in built.messages
    )
    assert "P0-1" in all_contents
    assert "P0-2" in all_contents
    assert "_select_history" in all_contents
    assert "Runner 工具循环" in all_contents
    # 当前用户消息永远在最后，且没有被摘要覆盖。
    assert built.messages[-1] == {
        "role": "user",
        "content": "请修复刚才的 P0 问题。",
    }
    # 无法整轮保留时，不应硬塞孤立的 tool 消息。
    assert all(message["role"] != "tool" for message in built.messages)
    assert built.stats.history_digest_count >= 1
    assert history == original


@pytest.mark.asyncio
async def test_latest_conclusion_reaches_provider_request(tmp_path: Path) -> None:
    """端到端：AgentLoop 实际发给 Provider 的上下文仍包含 P0-1 / P0-2。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    history = [
        {"role": "user", "content": "分析项目中的 P0 问题"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_read",
                    "name": "read_file",
                    "arguments": {"path": "mini_harness/agent/context.py"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read",
            "name": "read_file",
            "content": "x" * 20000,
        },
        {
            "role": "assistant",
            "content": (
                "P0-1: _select_history 导致关键结论丢失。\n"
                "P0-2: Runner 工具循环未重新检查上下文预算。"
            ),
        },
    ]
    manager.save(
        Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(history))
    )
    provider = MockProvider([LLMResponse(content="开始修复。")])
    registry = ToolRegistry()
    registry.register(AddTool())
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry),
        session_manager=manager,
        context_config=ContextConfig(
            max_context_tokens=2000, reserve_output_tokens=100
        ),
    )
    task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(
            InboundMessage(content="请修复刚才的 P0 问题。", sender="cli")
        )
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)
    finally:
        loop.stop()
        await task

    assert outbound.content == "开始修复。"
    sent = provider.received_messages[0]
    joined = "\n".join(str(message.get("content", "")) for message in sent)
    assert "P0-1" in joined
    assert "P0-2" in joined
    assert sent[-1] == {"role": "user", "content": "请修复刚才的 P0 问题。"}


@pytest.mark.asyncio
async def test_deterministic_fallback_keeps_previous_turn_conclusion(
    tmp_path: Path,
) -> None:
    """确定性回退也必须保留上一轮短结论，不因 20KB 工具结果整轮丢弃。"""
    workspace = make_workspace(tmp_path)
    config = ContextConfig(
        max_context_tokens=2000,
        reserve_output_tokens=100,
        max_tool_result_tokens=2048,
    )
    builder = ContextBuilder(
        workspace,
        config,
        tool_definitions=make_registry().get_definitions(),
    )
    history = [
        {"role": "user", "content": "分析项目中的 P0 问题"},
        {
            "role": "assistant",
            "content": "旧分析：" + "a" * 5300,
        },
        {"role": "assistant", "content": "OLD-P0: 关键结论不能丢。"},
    ]
    built = builder.build_context(
        history,
        {"role": "user", "content": "请修复刚才的 P0 问题。"},
    )

    provider = ToolLoopProvider(tool_calls=1, tool_size=20000)
    result = await AgentRunner(provider, make_registry()).run(
        AgentRunSpec(
            initial_messages=built.messages,
            max_iterations=3,
            context_config=config,
        )
    )

    assert result.stop_reason == "completed"
    final_request = provider.normal_requests[-1]
    all_contents = "\n".join(
        str(message.get("content", "")) for message in final_request
    )
    assert "OLD-P0" in all_contents
    # 工具真实结果仍完整保存在 Runner 消息里（Session 写入依据）。
    tool_contents = [
        str(message.get("content"))
        for message in result.messages
        if message.get("role") == "tool"
    ]
    assert tool_contents and len(tool_contents[0]) == 20006


# ---------------------------------------------------------------------------
# Test 2：同一回合连续工具调用，每次 Provider 请求前检查预算
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_is_checked_before_every_provider_request() -> None:
    """多次工具调用产生的每个 Provider 请求都经过预算检查与截断。"""
    provider = ToolLoopProvider(tool_calls=3, tool_size=4000)
    registry = make_registry()
    snapshots: list[Any] = []
    config = ContextConfig(
        max_context_tokens=2500,
        reserve_output_tokens=300,
        max_tool_result_tokens=300,
    )
    initial = [
        {"role": "system", "content": "你是测试助手。"},
        {"role": "user", "content": "连续执行多次工具并汇总。"},
    ]

    result = await AgentRunner(provider, registry).run(
        AgentRunSpec(
            initial_messages=initial,
            max_iterations=6,
            context_config=config,
            request_recorder=snapshots.append,
        )
    )

    assert result.stop_reason == "completed"
    # 3 次工具调用 + 1 次最终回答 = 4 次真实 Provider 请求。
    assert len(provider.normal_requests) == 4
    assert len(snapshots) == 4
    assert [snapshot.iteration for snapshot in snapshots] == [0, 1, 2, 3]
    assert provider.summary_calls == 0
    for snapshot in snapshots:
        assert snapshot.estimated_tokens <= snapshot.available_input_tokens
    # 空间足够时保留完整结果；真正超预算的后续请求才截断工具内容。
    for snapshot in snapshots[1:]:
        tool_messages = [
            message
            for message in snapshot.messages
            if message.get("role") == "tool"
        ]
        assert tool_messages
    pressured = [
        snapshot
        for snapshot in snapshots
        if snapshot.estimated_tokens > snapshot.available_input_tokens * 0.8
    ]
    for snapshot in pressured[:-1]:
        tool_messages = [
            message
            for message in snapshot.messages
            if message.get("role") == "tool"
        ]
        assert any(
            "truncated" in str(message["content"])
            for message in tool_messages
        )
    # Runner 的完整消息历史仍然保存原始 4000 字符工具结果。
    saved_tool_lengths = [
        len(str(message.get("content")))
        for message in result.messages
        if message.get("role") == "tool"
    ]
    assert saved_tool_lengths == [4006, 4006, 4006]


# ---------------------------------------------------------------------------
# Test 3：Compact 前后工具调用合法性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_preserves_tool_call_protocol() -> None:
    """Compact 后没有孤立 Tool Result，也没有缺结果的 tool_calls。"""
    provider = SummaryProvider()
    compactor = ContextCompactor(provider)
    old_turn = [
        {"role": "user", "content": "读取 context.py"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_old",
                    "name": "read_file",
                    "arguments": {"path": "mini_harness/agent/context.py"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_old",
            "name": "read_file",
            "content": "old result " * 200,
        },
        {"role": "assistant", "content": "旧结论：P0-1 已确认。"},
    ]
    delta = [
        {"role": "user", "content": "请继续修复。"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_new",
                    "name": "read_file",
                    "arguments": {"path": "mini_harness/agent/runner.py"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_new",
            "name": "read_file",
            "content": "new result " * 100,
        },
    ]
    messages = [
        {"role": "system", "content": "system"},
        *old_turn,
        *delta,
    ]

    outcome = await compactor.compact(
        messages,
        budget=3000,
        protected_messages=delta,
    )
    compacted = outcome.messages

    assert outcome is not None
    assert outcome.compacted is True
    assert compacted[0] == {"role": "system", "content": "system"}
    assert compacted[1]["compaction_summary"] is True
    assert compacted[-len(delta) :] == delta
    body = [message for message in compacted if message.get("role") != "system"]
    assert_tool_protocol(body)


# ---------------------------------------------------------------------------
# Test 4：Compact 失败安全回退
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_failure_returns_original_messages_safely() -> None:
    """摘要 Provider 失败时返回原始消息副本，并记录可理解错误。"""
    compactor = ContextCompactor(FailingSummaryProvider())
    old_history = [
        {"role": "user", "content": "旧问题 " + "a" * 3000},
        {"role": "assistant", "content": "旧回答 " + "b" * 3000},
    ]
    delta = [{"role": "user", "content": "当前请求：修复 P0。"}]
    messages = [
        {"role": "system", "content": "system"},
        *old_history,
        *delta,
    ]
    original = deepcopy(messages)

    outcome = await compactor.compact(messages, budget=500)
    compacted = outcome.messages

    assert compacted == original
    assert messages == original
    assert outcome is not None
    assert outcome.compacted is False
    assert "摘要调用失败" in str(outcome.error)
    assert compacted[-1] == {"role": "user", "content": "当前请求：修复 P0。"}


@pytest.mark.asyncio
async def test_governor_falls_back_to_deterministic_trim_when_summary_fails() -> None:
    """摘要失败后 Governor 仍用确定性截断压进预算，不调用 Provider。"""
    provider = FailingSummaryProvider()
    governor = ContextGovernor(
        provider,
        ContextConfig(
            max_context_tokens=1200,
            reserve_output_tokens=100,
            max_tool_result_tokens=2000,
        ),
    )
    history = [
        {"role": "user", "content": "旧问题 " + "a" * 4000},
        {"role": "assistant", "content": "旧结论 P0-1 " + "b" * 4000},
    ]
    delta = [{"role": "user", "content": "当前请求：继续修复 P0。"}]
    messages = [
        {"role": "system", "content": "s" * 50},
        *history,
        *delta,
    ]

    governed = await governor.prepare_request(
        messages,
        tools=None,
        iteration=1,
        protected_start=3,
    )

    assert governed.estimated_tokens <= governed.available_input_tokens
    assert governed.messages[-1] == {
        "role": "user",
        "content": "当前请求：继续修复 P0。",
    }
    assert governed.compaction_error is not None
    # 失败摘要被确定性回退替代；当前 Delta 未丢失。
    assert_tool_protocol(
        [message for message in governed.messages if message["role"] != "system"]
    )


class FailingSummaryToolProvider(ToolLoopProvider):
    """正常对话可用，但摘要 Provider 调用失败。"""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        if self._is_summary_request(messages):
            self.summary_calls += 1
            raise RuntimeError("summary down")
        return await super().chat(messages, tools=tools)


@pytest.mark.asyncio
async def test_summary_failure_does_not_break_session_or_turn(
    tmp_path: Path,
) -> None:
    """摘要失败时确定性回退，本轮正常完成，Session 保留完整原始结果。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    history: list[dict[str, Any]] = []
    for index in range(8):
        history.extend(
            [
                {"role": "user", "content": f"旧问题 {index} " + "a" * 200},
                {"role": "assistant", "content": f"旧回答 {index} " + "b" * 200},
            ]
        )
    manager.save(
        Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(history))
    )
    provider = FailingSummaryToolProvider(tool_calls=1, tool_size=5000)
    registry = ToolRegistry()
    registry.register(BulkTool())
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry),
        session_manager=manager,
        context_config=ContextConfig(
            max_context_tokens=1800,
            reserve_output_tokens=200,
            max_tool_result_tokens=600,
        ),
    )
    task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(
            InboundMessage(content="继续执行工具", sender="cli")
        )
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)
    finally:
        loop.stop()
        await task

    assert outbound.metadata.get("error") is None
    assert outbound.content == "最终回答：P0 分析完成。"
    assert provider.summary_calls >= 1
    saved = manager.load().messages
    assert saved[: len(history)] == history
    tool_messages = [
        message for message in saved if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert len(tool_messages[0]["content"]) == 5006


@pytest.mark.asyncio
async def test_summary_is_reused_across_provider_requests() -> None:
    """同一段已压缩历史在后续请求中复用 Summary，不再重复调用摘要模型。"""
    provider = ToolLoopProvider(tool_calls=0, summary="摘要：保留 P0-1。")
    config = ContextConfig(
        max_context_tokens=900,
        reserve_output_tokens=100,
        max_tool_result_tokens=500,
    )
    governor = ContextGovernor(provider, config)
    history = [
        {"role": "user", "content": "旧问题 " + "a" * 1500},
        {"role": "assistant", "content": "旧回答 " + "b" * 1500},
    ]
    delta = [{"role": "user", "content": "当前请求"}]
    messages = [
        {"role": "system", "content": "s" * 30},
        *history,
        *delta,
    ]
    first = await governor.prepare_request(
        messages,
        tools=None,
        iteration=1,
        protected_start=3,
    )
    assert first.compacted is True
    assert provider.summary_calls == 1
    summary_message = next(
        message for message in first.messages if message.get("compaction_summary")
    )

    # 同一历史 + 新增工具消息：历史签名不变，应复用 Summary。
    second = await governor.prepare_request(
        [
            *messages,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_x", "name": "bulk", "arguments": {}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_x", "name": "bulk", "content": "ok"},
        ],
        tools=None,
        iteration=2,
        protected_start=3,
    )
    assert provider.summary_calls == 1
    assert any(
        message.get("content") == summary_message.get("content")
        for message in second.messages
    )


# ---------------------------------------------------------------------------
# Test 5：Compact 后仍超预算，不无限循环
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_that_cannot_fit_raises_without_looping() -> None:
    """当前 Delta 自身超预算时快速失败，不反复调用 Compact。"""
    provider = ToolLoopProvider(tool_calls=0, summary="摘要")
    governor = ContextGovernor(
        provider,
        ContextConfig(max_context_tokens=400, reserve_output_tokens=100),
    )
    messages = [
        {"role": "system", "content": "s" * 30},
        {"role": "user", "content": "超" * 2000},
    ]

    with pytest.raises(ContextBudgetExceeded):
        await governor.prepare_request(
            messages,
            tools=None,
            iteration=0,
            protected_start=1,
        )

    # 没有进行无意义的 LLM 摘要，也没有无限循环。
    assert provider.summary_calls == 0
    assert governor.compaction_count == 0


@pytest.mark.asyncio
async def test_compactor_rejects_summary_that_does_not_reduce_context() -> None:
    """摘要不比原文短时放弃 Compact，而不是反复压缩同一段历史。"""
    provider = SummaryProvider(summary="S" * 5000)
    compactor = ContextCompactor(provider)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "当前问题"},
    ]

    outcome = await compactor.compact(messages, budget=1000)
    compacted = outcome.messages

    assert compacted == messages
    assert outcome is not None
    assert outcome.compacted is False
    assert "未减少" in str(outcome.error)
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Test 6：当前用户请求保护
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_user_message_is_never_compacted() -> None:
    """当前用户消息原样出现在 Compact 后请求的最后。"""
    provider = SummaryProvider("摘要：保留旧决策。")
    compactor = ContextCompactor(provider)
    current = {"role": "user", "content": "请修复刚才的 P0 问题。"}
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "旧问题 " + "a" * 3000},
        {"role": "assistant", "content": "旧回答 " + "b" * 3000},
        current,
    ]

    outcome = await compactor.compact(messages, budget=1000)
    compacted = outcome.messages

    assert compacted[-1] == current
    assert sum(
        1 for message in compacted if message.get("content") == current["content"]
    ) == 1
    # 当前用户文本没有进入摘要输入（避免被摘要覆盖）。
    summary_input = "\n".join(
        str(message.get("content", "")) for message in provider.calls[0]
    )
    assert current["content"] not in summary_input


# ---------------------------------------------------------------------------
# Test 7：Session 原始历史不被破坏
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_history_stays_complete_after_compaction(
    tmp_path: Path,
) -> None:
    """Compact 只影响 Provider 上下文；Session 保存完整原始工具结果。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    old_history = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
    ]
    manager.save(
        Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(old_history))
    )
    provider = ToolLoopProvider(tool_calls=1, tool_size=5000)
    registry = ToolRegistry()
    registry.register(BulkTool())
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry),
        session_manager=manager,
        context_config=ContextConfig(
            max_context_tokens=1400,
            reserve_output_tokens=200,
            max_tool_result_tokens=4000,
        ),
    )
    task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(InboundMessage(content="执行工具", sender="cli"))
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)
    finally:
        loop.stop()
        await task

    assert outbound.metadata.get("error") is None
    saved = manager.load().messages
    assert saved[: len(old_history)] == old_history
    tool_messages = [
        message for message in saved if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"] == "tool0:" + "x" * 5000
    assert all(message.get("role") != "system" for message in saved)
    assert all(
        message.get("compaction_summary") is None for message in saved
    )


@pytest.mark.asyncio
async def test_session_history_complete_after_successful_summary(
    tmp_path: Path,
) -> None:
    """成功 Compact 也只替换 Provider 上下文，Session 依旧保存完整原始结果。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    history: list[dict[str, Any]] = []
    for index in range(8):
        history.extend(
            [
                {"role": "user", "content": f"旧问题 {index} " + "a" * 200},
                {"role": "assistant", "content": f"旧回答 {index} " + "b" * 200},
            ]
        )
    manager.save(
        Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(history))
    )
    provider = ToolLoopProvider(tool_calls=1, tool_size=5000)
    registry = ToolRegistry()
    registry.register(BulkTool())
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, registry),
        session_manager=manager,
        context_config=ContextConfig(
            max_context_tokens=1800,
            reserve_output_tokens=200,
            max_tool_result_tokens=600,
        ),
    )
    task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(
            InboundMessage(content="继续执行工具", sender="cli")
        )
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)
    finally:
        loop.stop()
        await task

    assert outbound.metadata.get("error") is None
    assert provider.summary_calls >= 1
    final_request = provider.normal_requests[-1]
    assert any(
        message.get("compaction_summary") for message in final_request
    )
    saved = manager.load().messages
    assert saved[: len(history)] == history
    tool_messages = [
        message for message in saved if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"] == "tool0:" + "x" * 5000
    assert all(message.get("role") != "system" for message in saved)


# ---------------------------------------------------------------------------
# Test 8：/last 正确性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_reflects_final_real_provider_request(tmp_path: Path) -> None:
    """工具循环结束后，last_provider_request 等于最后一次真实请求。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    registry = make_registry()
    provider = ToolLoopProvider(tool_calls=2, tool_size=100)
    builder = ContextBuilder(
        workspace,
        ContextConfig(max_context_tokens=4096, reserve_output_tokens=512),
        tool_definitions=registry.get_definitions(),
    )
    agent_loop = AgentLoop(
        bus=MessageBus(),
        runner=AgentRunner(provider, registry),
        session_manager=manager,
        context_builder=builder,
    )
    task = asyncio.create_task(agent_loop.run())
    try:
        await agent_loop.bus.publish_inbound(
            InboundMessage(content="执行多次工具", sender="cli")
        )
        outbound = await asyncio.wait_for(
            agent_loop.bus.consume_outbound(),
            timeout=3.0,
        )
    finally:
        agent_loop.stop()
        await task

    assert outbound.content == "最终回答：P0 分析完成。"
    snapshot = builder.last_provider_request
    assert snapshot is not None
    # 2 次工具调用后的最后回答请求是 iteration=2、第 3 次真实请求。
    assert snapshot.iteration == 2
    assert snapshot.call_index == 3
    assert snapshot.messages == provider.normal_requests[-1]
    assert snapshot.tool_message_count >= 1
    assert snapshot.compacted is False
    assert snapshot.estimated_tokens <= snapshot.available_input_tokens
    assert snapshot.tool_definitions == registry.get_definitions()


# ---------------------------------------------------------------------------
# 边界测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compactor_no_history_is_safe_noop() -> None:
    """没有旧历史时不调用摘要模型，返回原消息。"""
    provider = SummaryProvider()
    compactor = ContextCompactor(provider)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "当前请求"},
    ]

    outcome = await compactor.compact(messages, budget=1000)
    result = outcome.messages

    assert result == messages
    assert provider.calls == []
    assert outcome is not None
    assert outcome.compacted is False


@pytest.mark.asyncio
async def test_compactor_no_tool_calls_still_protects_current_user() -> None:
    """无工具调用的普通历史也能 Compact，当前用户保持原样。"""
    provider = SummaryProvider("摘要：旧对话。")
    compactor = ContextCompactor(provider)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "旧问题 " + "a" * 500},
        {"role": "assistant", "content": "旧回答 " + "b" * 500},
        {"role": "user", "content": "当前问题"},
    ]

    outcome = await compactor.compact(messages, budget=600)
    result = outcome.messages

    assert result[-1] == {"role": "user", "content": "当前问题"}
    assert result[1]["compaction_summary"] is True


@pytest.mark.asyncio
async def test_governor_handles_empty_history_and_single_oversized_message() -> None:
    """空历史可正常检查预算；单条超大当前消息明确报错。"""
    provider = SummaryProvider()
    governor = ContextGovernor(
        provider,
        ContextConfig(max_context_tokens=500, reserve_output_tokens=100),
    )
    ok = await governor.prepare_request(
        [
            {"role": "system", "content": "s" * 20},
            {"role": "user", "content": "hello"},
        ],
        tools=None,
        iteration=0,
        protected_start=1,
    )
    assert ok.estimated_tokens <= ok.available_input_tokens

    with pytest.raises(ContextBudgetExceeded):
        await governor.prepare_request(
            [
                {"role": "system", "content": "s" * 20},
                {"role": "user", "content": "超" * 1000},
            ],
            tools=None,
            iteration=0,
            protected_start=1,
        )


@pytest.mark.asyncio
async def test_tiny_context_window_reports_clear_budget_error() -> None:
    """上下文窗口极小时不发送请求，返回明确 ContextBudgetExceeded。"""
    governor = ContextGovernor(
        SummaryProvider(),
        ContextConfig(max_context_tokens=300, reserve_output_tokens=50),
    )
    with pytest.raises(ContextBudgetExceeded) as exc_info:
        await governor.prepare_request(
            [
                {"role": "system", "content": "系统提示 " * 100},
                {"role": "user", "content": "hi"},
            ],
            tools=None,
            iteration=0,
            protected_start=1,
        )
    assert exc_info.value.estimated_tokens > exc_info.value.available_input_tokens


def test_split_message_regions_uses_stable_protected_start() -> None:
    """显式 protected_start 优先于“最后一条 user”推断。"""
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": "当前中间回答"},
        {"role": "user", "content": "内部收尾提示"},
    ]
    system, history, delta = split_message_regions(
        messages,
        protected_start=3,
    )
    assert system == [{"role": "system", "content": "system"}]
    assert history == [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
    ]
    assert delta[0] == {"role": "user", "content": "当前问题"}
    assert delta[-1] == {"role": "user", "content": "内部收尾提示"}


# ---------------------------------------------------------------------------
# 独立审查回归：128 Token 元数据、摘要请求预算、尾部未完成轮次
# ---------------------------------------------------------------------------


def test_truncated_tool_result_keeps_metadata_at_128_tokens() -> None:
    """极端 128 Token 回退也必须保留 tool/path/offset 元数据。"""
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_read",
                    "name": "read_file",
                    "arguments": {
                        "path": "src/a.py",
                        "offset": 10,
                        "limit": 20,
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read",
            "name": "read_file",
            "content": "x" * 5000,
        },
    ]
    truncated, count = truncate_tool_results(messages, 128)
    content = truncated[1]["content"]
    assert count == 1
    assert "truncated" in content.lower()
    assert "tool=read_file" in content
    assert "path=src/a.py" in content
    assert "offset=10" in content


def test_truncated_patch_result_keeps_affected_paths() -> None:
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "patch", "name": "apply_patch", "arguments": {"edits": [
                {"path": "a.py", "old_text": "old" * 100, "new_text": "new"},
                {"path": "b.py", "old_text": "old", "new_text": "new"},
            ]},
        }]},
        {"role": "tool", "tool_call_id": "patch", "name": "apply_patch", "content": "log" * 3000},
    ]
    truncated, count = truncate_tool_results(messages, 128)
    assert count == 1
    assert "a.py" in truncated[-1]["content"] and "b.py" in truncated[-1]["content"]
    assert "edit_count=2" in truncated[-1]["content"]
    assert "old_text" not in truncated[-1]["content"]


@pytest.mark.asyncio
async def test_summary_request_that_exceeds_budget_is_not_sent() -> None:
    """摘要请求自身超预算时直接跳过，不向 Provider 发送超预算调用。"""
    provider = ToolLoopProvider(tool_calls=0)
    compactor = ContextCompactor(provider)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "u" * 300},
        {"role": "assistant", "content": "a" * 300},
        {"role": "user", "content": "u" * 300},
        {"role": "assistant", "content": "a" * 300},
    ]

    outcome = await compactor.compact(messages, budget=200)
    compacted = outcome.messages

    assert compacted == messages
    assert provider.summary_calls == 0
    assert outcome is not None
    assert outcome.compacted is False
    assert "超过输入预算" in str(outcome.error)


@pytest.mark.asyncio
async def test_summary_larger_than_budget_is_rejected_by_compactor() -> None:
    """摘要本身无法放回预算时拒绝 Compact，保留原历史供确定性回退。"""
    provider = SummaryProvider(summary="S" * 5000)
    compactor = ContextCompactor(provider)
    messages: list[dict[str, Any]] = [{"role": "system", "content": "system"}]
    for index in range(10):
        messages.extend(
            [
                {"role": "user", "content": f"旧问题 {index} " + "a" * 900},
                {"role": "assistant", "content": f"旧回答 {index} " + "b" * 900},
            ]
        )
    messages.append({"role": "user", "content": "当前问题"})

    outcome = await compactor.compact(messages, budget=1000)
    compacted = outcome.messages

    assert compacted == messages
    assert provider.calls
    assert outcome is not None
    assert outcome.compacted is False
    assert "摘要本身估算" in str(outcome.error)


def test_unfinished_tail_does_not_hide_latest_completed_conclusion(
    tmp_path: Path,
) -> None:
    """尾部 user-only 未完成轮次不能让更早的已完成 P0 结论被丢弃。"""
    builder = ContextBuilder(
        make_workspace(tmp_path),
        ContextConfig(max_context_tokens=2000, reserve_output_tokens=100),
    )
    history = [
        {"role": "user", "content": "第一轮问题"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_read",
                    "name": "read_file",
                    "arguments": {"path": "src/a.py"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read",
            "name": "read_file",
            "content": "x" * 12000,
        },
        {"role": "assistant", "content": "OLD-P0: 关键结论不能丢。"},
        # 上一轮 Provider 返回空内容时 AgentLoop 会留下 user-only 轮次。
        {"role": "user", "content": "第二轮问题（上一轮空回答）"},
    ]

    built = builder.build_context(
        history,
        {"role": "user", "content": "请修复刚才的 P0 问题。"},
    )

    all_text = "\n".join(
        str(message.get("content", "")) for message in built.messages
    )
    assert "OLD-P0" in all_text
    assert built.stats.history_digest_count == 1


@pytest.mark.asyncio
async def test_governor_falls_back_when_summary_exceeds_budget() -> None:
    """摘要结果/摘要请求超预算时拒绝 Compact，并在原历史上确定性回退。"""
    provider = SummaryProvider(summary="S" * 3400)
    governor = ContextGovernor(
        provider,
        ContextConfig(
            max_context_tokens=1100,
            reserve_output_tokens=100,
            max_tool_result_tokens=500,
        ),
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": "S"}]
    for index in range(3):
        messages.extend(
            [
                {"role": "user", "content": f"旧问题{index} " + "u" * 900},
                {"role": "assistant", "content": f"旧回答{index} " + "a" * 900},
            ]
        )
    messages.append({"role": "assistant", "content": "OLD-P0: 上一轮关键结论。"})
    messages.extend(
        [
            {"role": "user", "content": "当前问题"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_big",
                        "name": "read_file",
                        "arguments": {"path": "big.py"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_big",
                "name": "read_file",
                "content": "x" * 20000,
            },
        ]
    )

    governed = await governor.prepare_request(
        messages,
        tools=None,
        iteration=1,
        protected_start=5,
    )

    assert governed.estimated_tokens <= governed.available_input_tokens
    assert governed.compacted is False
    assert any(
        token in str(governed.compaction_error)
        for token in ("超过输入预算", "未减少")
    )
    all_text = "\n".join(
        str(message.get("content", "")) for message in governed.messages
    )
    assert "OLD-P0" in all_text
