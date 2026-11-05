"""V2.2 Compaction / Idle / Dream 验收测试。

覆盖用户要求的 12 项验收场景。多数细节同时由单元测试覆盖，这里以
真实组件串联验证关键闭环。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from copy import deepcopy
from pathlib import Path

import pytest

from mini_harness.agent import (
    ContextBuilder,
    ContextConfig,
    ContextGovernor,
    IdleCompactionScheduler,
    SessionCompactor,
    SUMMARY_SYSTEM_PROMPT,
)
from mini_harness.memory import (
    DREAM_SYSTEM_PROMPT,
    DreamConfig,
    DreamManager,
    MemoryStore,
)
from mini_harness.providers.base import LLMProvider, LLMResponse
from mini_harness.session import DEFAULT_SESSION_KEY, Session, SessionManager


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def complete_turn(index: int, *, size: int = 1200) -> list[dict]:
    return [
        {"role": "user", "content": f"第 {index} 轮问题"},
        {"role": "assistant", "content": f"第 {index} 轮结论：" + "x" * size},
    ]


class DualProvider(LLMProvider):
    """同时响应 Compaction 摘要与 Dream JSON 的测试 Provider。"""

    def __init__(
        self,
        *,
        summary: str = "摘要：保留 P0-1 与项目路径。",
        dream_payload: dict | str | None = None,
    ) -> None:
        self.summary = summary
        self.dream_payload = dream_payload or {
            "add": [{"section": "项目决策", "text": "Session 保存完整原始历史"}],
            "update": [],
            "remove": [],
        }
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None) -> LLMResponse:
        self.calls.append(deepcopy(messages))
        system = messages[0].get("content", "")
        if system == SUMMARY_SYSTEM_PROMPT:
            return LLMResponse(content=self.summary)
        if system == DREAM_SYSTEM_PROMPT:
            payload = self.dream_payload
            if isinstance(payload, str):
                return LLMResponse(content=payload)
            return LLMResponse(content=json.dumps(payload, ensure_ascii=False))
        return LLMResponse(content="正常回答")


class FailingProvider(LLMProvider):
    async def chat(self, messages, tools=None) -> LLMResponse:
        raise RuntimeError("provider down")


class CapturingProvider(DualProvider):
    """记录调用并可按 system prompt 返回固定内容。"""


# ---------------------------------------------------------------------------
# 1. Token 达到已有阈值时触发压缩
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_pressure_triggers_compaction() -> None:
    provider = DualProvider(summary="摘要：P0 结论已保留。")
    config = ContextConfig(max_context_tokens=1500, reserve_output_tokens=200)
    governor = ContextGovernor(provider, config)
    history = []
    for index in range(3):
        history.extend(
            [
                {"role": "user", "content": f"历史问题 {index}"},
                {"role": "assistant", "content": "a" * 1800},
            ]
        )
    messages = [
        {"role": "system", "content": "system"},
        *history,
        {"role": "user", "content": "当前请求"},
    ]
    governed = await governor.prepare_request(
        messages,
        tools=None,
        iteration=1,
        protected_start=len(messages) - 1,
    )
    assert governed.compacted is True
    assert governed.summarized_messages > 0
    assert any(m.get("compaction_summary") for m in governed.messages)
    assert governed.messages[-1] == {"role": "user", "content": "当前请求"}
    assert governed.estimated_tokens <= governed.available_input_tokens


# ---------------------------------------------------------------------------
# 2/3. Idle Compaction：15 分钟触发；活跃 Session 跳过
# ---------------------------------------------------------------------------


def _make_idle_session(tmp_path: Path) -> tuple[SessionManager, SessionCompactor]:
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=deepcopy([*complete_turn(1), *complete_turn(2)]),
        )
    )
    old = time.time() - 16 * 60
    os.utime(manager.path, (old, old))
    compactor = SessionCompactor(
        DualProvider(),
        manager,
        memory_store=MemoryStore(workspace),
        context_config=ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
    )
    return manager, compactor


@pytest.mark.asyncio
async def test_idle_compaction_after_15_minutes(tmp_path: Path) -> None:
    manager, compactor = _make_idle_session(tmp_path)
    scheduler = IdleCompactionScheduler(
        compactor,
        manager,
        idle_after_minutes=15,
        check_interval_seconds=60,
    )
    result = await scheduler.check_once()
    assert result is not None
    assert result.status == "compacted"
    restored = manager.load()
    assert restored.summary
    assert restored.summary_boundary > 0


@pytest.mark.asyncio
async def test_active_session_is_not_idle_compacted(tmp_path: Path) -> None:
    manager, compactor = _make_idle_session(tmp_path)
    scheduler = IdleCompactionScheduler(
        compactor,
        manager,
        idle_after_minutes=15,
        check_interval_seconds=60,
    )
    scheduler.mark_active(DEFAULT_SESSION_KEY)
    assert await scheduler.check_once() is None
    assert manager.load().summary == ""


# ---------------------------------------------------------------------------
# 4. `/compact` 手动压缩（SessionCompactor 层）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_compaction_ignores_time_and_budget(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=deepcopy([*complete_turn(1), *complete_turn(2)]),
        )
    )
    provider = DualProvider()
    compactor = SessionCompactor(provider, manager, context_config=ContextConfig())
    result = await compactor.compact(reason="manual")
    assert result.status == "compacted"
    assert result.reason == "manual"
    assert result.boundary == 4


# ---------------------------------------------------------------------------
# 5. 压缩后重启恢复 Summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_survives_restart(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    data_root = tmp_path / "data"
    manager = SessionManager(workspace, data_root=data_root)
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=deepcopy([*complete_turn(1), *complete_turn(2)]),
        )
    )
    await SessionCompactor(DualProvider(), manager).compact(reason="manual")

    restarted = SessionManager(workspace, data_root=data_root).load()
    assert restarted.summary
    assert restarted.summary_boundary == 4

    builder = ContextBuilder(
        workspace,
        ContextConfig(max_context_tokens=8192, reserve_output_tokens=512),
    )
    built = builder.build_context(
        restarted.messages,
        {"role": "user", "content": "继续"},
        summary=restarted.summary,
        summary_boundary=restarted.summary_boundary,
    )
    joined = "\n".join(str(m.get("content", "")) for m in built.messages)
    assert restarted.summary in joined
    assert "第 1 轮问题" not in joined
    assert built.messages[-1] == {"role": "user", "content": "继续"}


# ---------------------------------------------------------------------------
# 6. 工具调用与结果关系不被破坏
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_pairing_survives_compaction(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    history = [
        {"role": "user", "content": "调用工具"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "工具结果：" + "y" * 3000},
        {"role": "assistant", "content": "工具调用完成"},
    ]
    manager.save(Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(history)))
    await SessionCompactor(DualProvider(), manager).compact(reason="manual")

    restored = manager.load()
    builder = ContextBuilder(workspace, ContextConfig(max_context_tokens=8192, reserve_output_tokens=512))
    built = builder.build_context(
        restored.messages,
        {"role": "user", "content": "下一轮"},
        summary=restored.summary,
        summary_boundary=restored.summary_boundary,
    )
    # 摘要替换了已归档工具轮，请求中不应出现孤立 tool 消息。
    assert all(m["role"] != "tool" for m in built.messages)
    assert built.messages[1]["compaction_summary"] is True
    assert built.messages[-1] == {"role": "user", "content": "下一轮"}


# ---------------------------------------------------------------------------
# 7. 已归档历史不会重复压缩
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archived_history_is_not_recompacted(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=deepcopy([*complete_turn(1), *complete_turn(2)]),
        )
    )
    provider = DualProvider()
    compactor = SessionCompactor(provider, manager)
    first = await compactor.compact(reason="manual")
    assert first.status == "compacted"
    calls_after_first = len(provider.calls)

    second = await compactor.compact(reason="manual")
    assert second.status == "nothing_to_compact"
    assert len(provider.calls) == calls_after_first


# ---------------------------------------------------------------------------
# 8/9/10/11. Dream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dream_without_new_history_skips_llm(tmp_path: Path) -> None:
    store = MemoryStore(make_workspace(tmp_path))
    provider = DualProvider()
    dream = DreamManager(provider, store, DreamConfig())
    result = await dream.run()
    assert result.processed_entries == 0
    assert result.llm_called is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_dream_success_advances_cursor(tmp_path: Path) -> None:
    store = MemoryStore(make_workspace(tmp_path))
    store.append_history("用户偏好：使用中文回答", session_key="cli:main", checkpoint_id="cp-1")
    provider = DualProvider()
    dream = DreamManager(provider, store, DreamConfig())
    result = await dream.run()
    assert result.error is None
    assert result.processed_entries == 1
    assert result.memory_updated is True
    assert result.cursor == 1
    assert store.get_dream_cursor() == 1
    assert "Session 保存完整原始历史" in store.read_memory()


@pytest.mark.asyncio
async def test_dream_failure_keeps_cursor(tmp_path: Path) -> None:
    store = MemoryStore(make_workspace(tmp_path))
    store.append_history("摘要一", session_key="cli:main", checkpoint_id="cp-1")
    dream = DreamManager(FailingProvider(), store, DreamConfig())
    result = await dream.run()
    assert result.error is not None
    assert result.cursor == 0
    assert store.get_dream_cursor() == 0
    assert store.read_memory() == ""


@pytest.mark.asyncio
async def test_dream_restart_does_not_reprocess(tmp_path: Path) -> None:
    store = MemoryStore(make_workspace(tmp_path))
    store.append_history("摘要一", session_key="cli:main", checkpoint_id="cp-1")
    first_provider = DualProvider()
    first = DreamManager(first_provider, store, DreamConfig())
    assert (await first.run()).cursor == 1

    second_provider = DualProvider()
    second = DreamManager(second_provider, store, DreamConfig())
    result = await second.run()
    assert result.processed_entries == 0
    assert result.llm_called is False
    assert second_provider.calls == []


# ---------------------------------------------------------------------------
# 12. Compaction 与 Dream 并发运行安全
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_and_dream_concurrent_are_safe(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=deepcopy([*complete_turn(1), *complete_turn(2)]),
        )
    )
    store = MemoryStore(workspace)
    store.append_history("历史摘要一", session_key="cli:main", checkpoint_id="pre-1")
    provider = DualProvider()
    compactor = SessionCompactor(provider, manager, memory_store=store)
    dream = DreamManager(provider, store, DreamConfig())

    compaction_result, dream_result = await asyncio.gather(
        compactor.compact(reason="manual"),
        dream.run(),
    )

    assert compaction_result.status == "compacted"
    assert dream_result.error is None
    assert manager.load().summary
    # history.jsonl 的 cursor 单调、无重复；Dream cursor 只指向已处理记录。
    records = store.read_history()
    cursors = [record.cursor for record in records]
    assert cursors == sorted(set(cursors))
    assert store.get_dream_cursor() >= 1
    assert store.get_dream_cursor() <= max(cursors)
    # MEMORY.md 合法可读，Session JSON 合法可读。
    assert "# 长期记忆" in store.read_memory()
    json.loads(manager.path.read_text(encoding="utf-8"))
