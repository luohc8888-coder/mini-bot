"""AgentLoop 对持久化 Summary Checkpoint 的恢复与保存测试（V2.2）。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from mini_harness.agent import AgentLoop, AgentRunner, ContextBuilder, ContextConfig
from mini_harness.bus import InboundMessage, MessageBus
from mini_harness.providers.base import LLMResponse
from mini_harness.providers.mock import MockProvider
from mini_harness.session import DEFAULT_SESSION_KEY, Session, SessionManager
from mini_harness.tools import ToolRegistry


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.mark.asyncio
async def test_loop_sends_summary_instead_of_archived_history_and_keeps_fields(
    tmp_path: Path,
) -> None:
    """重启后请求包含摘要+未归档历史；回合结束后 summary 字段不丢失。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    archived = [
        {"role": "user", "content": "旧请求：分析项目"},
        {"role": "assistant", "content": "旧回答：P0-1 已定位。"},
    ]
    unarchived = [
        {"role": "user", "content": "新请求：继续修复"},
        {"role": "assistant", "content": "新回答：修复中。"},
    ]
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=deepcopy([*archived, *unarchived]),
            summary="持久化摘要：P0-1 已定位，继续修复。",
            summary_boundary=2,
            summary_checkpoint_id="checkpoint-001",
        )
    )

    provider = MockProvider([LLMResponse(content="修复完成。")])
    builder = ContextBuilder(
        workspace,
        ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
        tool_definitions=ToolRegistry().get_definitions(),
    )
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, ToolRegistry()),
        session_manager=manager,
        context_builder=builder,
        context_config=ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
    )
    task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(
            InboundMessage(content="请继续。", sender="cli")
        )
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)
    finally:
        loop.stop()
        await task

    assert outbound.content == "修复完成。"
    sent = provider.received_messages[0]
    joined = "\n".join(str(message.get("content", "")) for message in sent)
    assert "持久化摘要：P0-1 已定位" in joined
    assert "旧请求：分析项目" not in joined
    assert "旧回答：P0-1 已定位。" not in joined
    assert "新请求：继续修复" in joined
    assert sent[-1] == {"role": "user", "content": "请继续。"}

    restored = manager.load()
    assert restored.summary == "持久化摘要：P0-1 已定位，继续修复。"
    assert restored.summary_boundary == 2
    assert restored.summary_checkpoint_id == "checkpoint-001"
    # 原始历史完整保留，且新增回合已 append（4 条初始 + 2 条新增）。
    assert len(restored.messages) == 6
    assert restored.messages[0]["content"] == "旧请求：分析项目"
    assert restored.messages[-1]["content"] == "修复完成。"


@pytest.mark.asyncio
async def test_loop_without_summary_is_backward_compatible(tmp_path: Path) -> None:
    """旧 Session 无摘要字段时行为不变，请求包含全部历史。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=[
                {"role": "user", "content": "第一轮"},
                {"role": "assistant", "content": "第一轮回答"},
            ],
        )
    )
    provider = MockProvider([LLMResponse(content="第二轮回答")])
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        runner=AgentRunner(provider, ToolRegistry()),
        session_manager=manager,
        context_config=ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
    )
    task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(InboundMessage(content="第二轮", sender="cli"))
        await asyncio.wait_for(bus.consume_outbound(), timeout=3.0)
    finally:
        loop.stop()
        await task

    sent = provider.received_messages[0]
    assert "第一轮" in "\n".join(str(m.get("content", "")) for m in sent)
    restored = manager.load()
    assert restored.summary == ""
    assert restored.summary_boundary == 0
    assert len(restored.messages) == 4
