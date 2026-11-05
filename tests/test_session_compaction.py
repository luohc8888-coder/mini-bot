"""持久化 SessionCompactor 的回归测试。

覆盖：
1. 手动压缩成功：Session Summary / boundary / checkpoint 与 Memory Archive；
2. 重启后由新的 SessionManager 恢复摘要；
3. 已归档历史不重复压缩、不重复调用 LLM；
4. 旧 summary 合并进新历史的摘要输入；
5. Provider 失败、非法摘要、未减少 Token 时 Session 完全不变；
6. Tool Call / Tool Result 配对在压缩后保持完整；
7. 不完整轮次停住、无新历史不调用 LLM；
8. 同一 Session 并发 compact 返回 busy，不重复调用 LLM。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mini_harness.agent.compaction import SUMMARY_SYSTEM_PROMPT
from mini_harness.agent.context_types import ContextConfig
from mini_harness.agent.session_compaction import (
    PersistentCompactionResult,
    SessionCompactor,
)
from mini_harness.memory.store import MemoryStore, MemoryStoreError
from mini_harness.providers.base import LLMProvider, LLMResponse
from mini_harness.session import DEFAULT_SESSION_KEY, Session, SessionManager


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def make_manager(tmp_path: Path) -> SessionManager:
    return SessionManager(
        make_workspace(tmp_path),
        data_root=tmp_path / "data",
    )


def long_text(prefix: str, repeat: int = 60) -> str:
    """生成足够长的消息正文，保证摘要确实能减少上下文 Token。"""
    return prefix + "用户需求与背景说明。" * repeat


def make_complete_turn(index: int) -> list[dict[str, Any]]:
    """构造一轮完整的“用户 → 最终 assistant 回答”。"""
    return [
        {"role": "user", "content": long_text(f"第 {index} 轮用户请求：")},
        {"role": "assistant", "content": long_text(f"第 {index} 轮最终回答：")},
    ]


def make_tool_turn(index: int) -> list[dict[str, Any]]:
    """构造一轮包含完整 Tool Call / Tool Result 的合法历史。"""
    call_id = f"call_bulk_{index}"
    return [
        {
            "role": "user",
            "content": long_text(f"第 {index} 轮：请调用工具检查项目状态。"),
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "name": "bulk",
                    "arguments": {"size": 4096, "label": f"tool{index}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "bulk",
            "content": "工具结果：" + "x" * 8000,
        },
        {
            "role": "assistant",
            "content": f"第 {index} 轮最终回答：工具结果已确认，P0-1 已修复。",
        },
    ]


def assert_tool_protocol(messages: list[dict[str, Any]]) -> None:
    """断言消息序列没有孤立 tool 结果或缺失结果的 tool_calls。"""
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "user":
            assert not pending
            pending = set()
        elif role == "assistant":
            assert not pending
            pending = {
                str(call["id"]) for call in message.get("tool_calls") or []
            }
        elif role == "tool":
            tool_call_id = str(message.get("tool_call_id"))
            assert tool_call_id in pending
            pending.remove(tool_call_id)
        else:  # pragma: no cover - 测试历史不包含 system
            raise AssertionError(f"未知角色：{role}")
    assert not pending


class RecordingSummaryProvider(LLMProvider):
    """记录每次摘要请求的 Provider；可返回多版摘要或直接抛错。"""

    def __init__(
        self,
        summaries: list[str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.summaries = list(
            summaries or ["摘要：保留 P0-1 与下一步验证计划。"]
        )
        self.error = error
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append(deepcopy(messages))
        if self.error is not None:
            raise self.error
        index = min(len(self.calls) - 1, len(self.summaries) - 1)
        return LLMResponse(content=self.summaries[index])


class BlockingSummaryProvider(LLMProvider):
    """摘要请求会一直等待，直到测试显式放行；用于并发 busy 场景。"""

    def __init__(self, summary: str = "并发摘要：P0-1 已修复。") -> None:
        self.summary = summary
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return LLMResponse(content=self.summary)


def save_messages(
    manager: SessionManager,
    messages: list[dict[str, Any]],
) -> None:
    manager.save(
        Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(messages))
    )


# ---------------------------------------------------------------------------
# 基础成功路径
# ---------------------------------------------------------------------------


def test_manual_compaction_saves_checkpoint_and_archive(
    tmp_path: Path,
) -> None:
    """手动压缩成功后 Session 与 Memory Archive 都应出现新 checkpoint。"""
    manager = make_manager(tmp_path)
    store = MemoryStore(manager.workspace)
    messages = [*make_complete_turn(1), *make_complete_turn(2)]
    save_messages(manager, messages)
    provider = RecordingSummaryProvider(["第一版摘要：P0-1 已修复。"])
    compactor = SessionCompactor(provider, manager, memory_store=store)

    result = asyncio.run(compactor.compact(reason="manual"))

    assert isinstance(result, PersistentCompactionResult)
    assert result.ok is True
    assert result.status == "compacted"
    assert result.reason == "manual"
    assert result.summarized_messages == len(messages)
    assert result.boundary == len(messages)
    assert result.summary == "第一版摘要：P0-1 已修复。"
    assert result.archive_cursor == 1
    assert result.error is None
    assert len(provider.calls) == 1
    assert provider.calls[0][0]["content"] == SUMMARY_SYSTEM_PROMPT

    # checkpoint id 必须是 session_key + boundary + summary 的 SHA-256 前 16 位。
    checkpoint_payload = json.dumps(
        [DEFAULT_SESSION_KEY, len(messages), "第一版摘要：P0-1 已修复。"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(checkpoint_payload.encode("utf-8")).hexdigest()[:16]
    assert result.checkpoint_id == expected
    assert len(result.checkpoint_id) == 16

    restored = manager.load()
    # 完整原始历史必须保留，压缩只移动 summary_boundary。
    assert restored.messages == messages
    assert restored.summary == result.summary
    assert restored.summary_boundary == len(messages)
    assert restored.summary_checkpoint_id == result.checkpoint_id

    records = store.read_history()
    assert len(records) == 1
    assert records[0].content == result.summary
    assert records[0].checkpoint_id == result.checkpoint_id
    assert records[0].session_key == DEFAULT_SESSION_KEY

    # 同一 checkpoint 重复归档必须幂等：不再新增记录。
    assert (
        store.append_history(
            result.summary,
            session_key=DEFAULT_SESSION_KEY,
            checkpoint_id=result.checkpoint_id,
        )
        == 1
    )
    assert len(store.read_history()) == 1


def test_summary_restored_by_new_session_manager(tmp_path: Path) -> None:
    """模拟重启：新 SessionManager 从同一数据目录恢复 Summary。"""
    manager = make_manager(tmp_path)
    store = MemoryStore(manager.workspace)
    messages = [*make_complete_turn(1), *make_complete_turn(2)]
    save_messages(manager, messages)
    provider = RecordingSummaryProvider(["重启后仍有效的摘要。"])
    compactor = SessionCompactor(provider, manager, memory_store=store)
    result = asyncio.run(compactor.compact())

    restarted = SessionManager(
        manager.workspace,
        data_root=manager.data_root,
    )
    restored = restarted.load()

    assert restored.summary == result.summary == "重启后仍有效的摘要。"
    assert restored.summary_boundary == len(messages)
    assert restored.summary_checkpoint_id == result.checkpoint_id
    assert restored.messages == messages
    # 新实例再次压缩应无新历史，且不调用 LLM。
    calls_after_first = len(provider.calls)
    again = asyncio.run(SessionCompactor(provider, restarted).compact())
    assert again.status == "nothing_to_compact"
    assert len(provider.calls) == calls_after_first


def test_second_compaction_is_noop_without_llm(tmp_path: Path) -> None:
    """已归档 Session 第二次 compact 返回 nothing_to_compact 且不调 LLM。"""
    manager = make_manager(tmp_path)
    messages = [*make_complete_turn(1)]
    save_messages(manager, messages)
    provider = RecordingSummaryProvider(["第一版摘要。"])
    compactor = SessionCompactor(provider, manager)

    first = asyncio.run(compactor.compact())
    assert first.status == "compacted"
    calls_after_first = len(provider.calls)
    state_after_first = manager.load()

    second = asyncio.run(compactor.compact())

    assert second.ok is True
    assert second.status == "nothing_to_compact"
    assert second.summarized_messages == 0
    assert len(provider.calls) == calls_after_first
    assert manager.load() == state_after_first


def test_previous_summary_is_merged_into_new_history(tmp_path: Path) -> None:
    """第二次压缩必须把上一版 summary 一起作为摘要输入。"""
    manager = make_manager(tmp_path)
    store = MemoryStore(manager.workspace)
    first_turn = make_complete_turn(1)
    save_messages(manager, first_turn)
    provider = RecordingSummaryProvider(
        ["第一版摘要：项目目标 A。", "第二版摘要：项目目标 A 与新结论 B。"]
    )
    compactor = SessionCompactor(provider, manager, memory_store=store)

    first = asyncio.run(compactor.compact())
    assert first.summary == "第一版摘要：项目目标 A。"

    # 模拟新回合追加：AgentLoop 会加载完整历史后保存，保留 summary 字段。
    session = manager.load()
    second_turn = make_complete_turn(2)
    session.messages.extend(second_turn)
    manager.save(session)

    second = asyncio.run(compactor.compact())

    assert second.status == "compacted"
    assert second.summary == "第二版摘要：项目目标 A 与新结论 B。"
    assert second.boundary == len(first_turn) + len(second_turn)
    assert len(provider.calls) == 2
    # ContextCompactor 的 transcript 中应包含上一版 summary 的纯正文。
    transcript = provider.calls[1][1]["content"]
    assert "第一版摘要：项目目标 A。" in transcript

    records = store.read_history()
    assert [record.content for record in records] == [
        first.summary,
        second.summary,
    ]
    assert manager.load().summary == second.summary
    assert manager.load().messages == [*first_turn, *second_turn]


# ---------------------------------------------------------------------------
# 失败路径：Session 完全不变
# ---------------------------------------------------------------------------


def test_provider_failure_leaves_session_unchanged(tmp_path: Path) -> None:
    """摘要 Provider 失败时 Session 文件字节级不变，也不产生归档。"""
    manager = make_manager(tmp_path)
    store = MemoryStore(manager.workspace)
    save_messages(manager, make_complete_turn(1))
    before = manager.path.read_bytes()
    provider = RecordingSummaryProvider(error=RuntimeError("summary down"))
    compactor = SessionCompactor(provider, manager, memory_store=store)

    result = asyncio.run(compactor.compact())

    assert result.ok is False
    assert result.status == "failed"
    assert result.error is not None and "摘要调用失败" in result.error
    assert manager.path.read_bytes() == before
    assert not store.history_file.exists()


def test_empty_summary_leaves_session_unchanged(tmp_path: Path) -> None:
    """空摘要属于非法结果，Session 不变。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn(1))
    before = manager.path.read_bytes()
    provider = RecordingSummaryProvider(["   "])
    compactor = SessionCompactor(provider, manager)

    result = asyncio.run(compactor.compact())

    assert result.status == "failed"
    assert result.error is not None
    assert manager.path.read_bytes() == before


def test_non_reducing_summary_leaves_session_unchanged(
    tmp_path: Path,
) -> None:
    """摘要没有减少 Token 时必须放弃本次压缩，Session 不变。"""
    manager = make_manager(tmp_path)
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]
    save_messages(manager, messages)
    before = manager.path.read_bytes()
    provider = RecordingSummaryProvider(["摘要" * 2000])
    compactor = SessionCompactor(provider, manager)

    result = asyncio.run(compactor.compact())

    assert result.status == "failed"
    assert result.error is not None and "未减少" in result.error
    assert manager.path.read_bytes() == before


def test_unfit_first_turn_fails_without_llm(tmp_path: Path) -> None:
    """最小预算连一轮都放不下时安全失败：不调用 LLM、Session 不变。"""
    manager = make_manager(tmp_path)
    messages = [
        {"role": "user", "content": "u" * 4000},
        {"role": "assistant", "content": "a" * 4000},
    ]
    save_messages(manager, messages)
    before = manager.path.read_bytes()
    provider = RecordingSummaryProvider(["不会用到的摘要。"])
    compactor = SessionCompactor(
        provider,
        manager,
        context_config=ContextConfig(
            max_context_tokens=1024,
            reserve_output_tokens=512,
        ),
    )

    result = asyncio.run(compactor.compact())

    assert result.status == "failed"
    assert result.error is not None and "不足以完整覆盖" in result.error
    assert provider.calls == []
    assert manager.path.read_bytes() == before


def test_archive_failure_does_not_undo_session_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory Archive 失败不影响已成功保存的 Session Summary。"""
    manager = make_manager(tmp_path)
    store = MemoryStore(manager.workspace)
    messages = [*make_complete_turn(1)]
    save_messages(manager, messages)
    provider = RecordingSummaryProvider(["归档失败但 Session 已保存。"])
    compactor = SessionCompactor(provider, manager, memory_store=store)

    def failing_append(*args: Any, **kwargs: Any) -> int:
        raise MemoryStoreError("disk full (simulated)")

    monkeypatch.setattr(store, "append_history", failing_append)

    result = asyncio.run(compactor.compact())

    assert result.ok is True
    assert result.status == "compacted"
    assert result.archive_cursor is None
    assert result.error is not None and "归档失败" in result.error
    restored = manager.load()
    assert restored.summary == result.summary
    assert restored.summary_boundary == len(messages)


# ---------------------------------------------------------------------------
# 轮次选择与协议
# ---------------------------------------------------------------------------


def test_incomplete_final_turn_is_left_for_later(tmp_path: Path) -> None:
    """末尾未完成的用户请求必须保留，不进入本次摘要。"""
    manager = make_manager(tmp_path)
    complete = make_complete_turn(1)
    incomplete = [{"role": "user", "content": "新的请求尚未得到回答。"}]
    messages = [*complete, *incomplete]
    save_messages(manager, messages)
    provider = RecordingSummaryProvider(["只摘要已完成轮次。"])
    compactor = SessionCompactor(provider, manager)

    result = asyncio.run(compactor.compact())

    assert result.status == "compacted"
    assert result.summarized_messages == len(complete)
    assert result.boundary == len(complete)
    restored = manager.load()
    assert restored.summary_boundary == len(complete)
    assert restored.messages == messages
    # 剩余的是不完整轮次，因此已经没有可归档的完整历史。
    assert compactor.has_unarchived_history(restored) is False

    # 未完成轮次仍未归档：再次 compress 时不能被选择，但也不会调用 LLM。
    calls_before = len(provider.calls)
    again = asyncio.run(compactor.compact())
    assert again.status == "nothing_to_compact"
    assert len(provider.calls) == calls_before


def test_only_incomplete_history_is_nothing_to_compact(
    tmp_path: Path,
) -> None:
    """只有不完整轮次时返回 nothing_to_compact，绝不调用 LLM。"""
    manager = make_manager(tmp_path)
    save_messages(
        manager,
        [{"role": "user", "content": "只有当前请求，还没有回答。"}],
    )
    provider = RecordingSummaryProvider()
    compactor = SessionCompactor(provider, manager)

    assert compactor.has_unarchived_history() is False
    result = asyncio.run(compactor.compact())

    assert result.status == "nothing_to_compact"
    assert result.ok is True
    assert provider.calls == []


def test_empty_session_is_nothing_to_compact(tmp_path: Path) -> None:
    """Session 不存在/为空时不调用 LLM。"""
    manager = make_manager(tmp_path)
    provider = RecordingSummaryProvider()
    compactor = SessionCompactor(provider, manager)

    result = asyncio.run(compactor.compact())

    assert result.status == "nothing_to_compact"
    assert provider.calls == []


def test_tool_protocol_stays_valid_after_compaction(tmp_path: Path) -> None:
    """包含 Tool Call / Tool Result 的完整轮次可以归档且配对不被破坏。"""
    manager = make_manager(tmp_path)
    messages = [*make_complete_turn(1), *make_tool_turn(2)]
    save_messages(manager, messages)
    provider = RecordingSummaryProvider(["工具调用轮次已归档。"])
    compactor = SessionCompactor(provider, manager)

    result = asyncio.run(compactor.compact())

    assert result.status == "compacted"
    restored = manager.load()
    assert restored.messages == messages
    assert restored.summary_boundary == len(messages)
    assert_tool_protocol(restored.messages)


def test_has_unarchived_history_reports_complete_turns(tmp_path: Path) -> None:
    """has_unarchived_history 只认可完整、未归档的用户轮次。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn(1))
    provider = RecordingSummaryProvider(["摘要。"])
    compactor = SessionCompactor(provider, manager)

    assert compactor.has_unarchived_history() is True
    asyncio.run(compactor.compact())
    assert compactor.has_unarchived_history() is False


# ---------------------------------------------------------------------------
# 大历史：只推进被完整摘要覆盖的前缀
# ---------------------------------------------------------------------------


def test_large_history_advances_only_fully_summarized_prefix(
    tmp_path: Path,
) -> None:
    """>100KB 多轮历史 + 小预算时，boundary 只覆盖真正进入 transcript 的轮次。"""
    manager = make_manager(tmp_path)
    messages: list[dict[str, Any]] = []
    for index in range(6):
        messages.extend(
            [
                {
                    "role": "user",
                    "content": f"TURN{index}USER" + "u" * 20_000,
                },
                {
                    "role": "assistant",
                    "content": f"TURN{index}ASSISTANT" + "a" * 20_000,
                },
            ]
        )
    save_messages(manager, messages)
    assert sum(len(message["content"]) for message in messages) > 100_000

    provider = RecordingSummaryProvider(
        [f"SUMMARY-{index}" for index in range(10)]
    )
    # 可用输入预算只有 4096 tokens，而 6 轮原始内容约 24 万字符；
    # ContextCompactor 的 transcript 只能完整容纳前缀轮次。
    compactor = SessionCompactor(
        provider,
        manager,
        context_config=ContextConfig(
            max_context_tokens=16384,
            reserve_output_tokens=12288,
        ),
    )

    first = asyncio.run(compactor.compact())
    assert first.status == "compacted"
    assert 0 < first.summarized_messages < len(messages)
    assert first.summarized_messages == first.boundary
    transcript = provider.calls[0][1]["content"]
    # _build_transcript 不能出现“丢弃最旧消息”的标记，否则说明覆盖不完整。
    assert "[... 较早的消息因摘要输入预算已省略 ...]" not in transcript
    covered = [
        index
        for index in range(6)
        if f"TURN{index}USER" in transcript
    ]
    # 被摘要的轮次必须是连续前缀，且 boundary 与真实覆盖轮次数一致。
    assert covered == list(range(len(covered)))
    assert 0 < len(covered) < 6
    assert first.summarized_messages == len(covered) * 2
    assert first.boundary == len(covered) * 2

    # 下一次 compact 必须从剩余轮次继续，而不是跳过它们。
    boundary = first.boundary
    for step in range(1, 10):
        result = asyncio.run(compactor.compact())
        if result.status == "nothing_to_compact":
            break
        assert result.status == "compacted"
        assert result.boundary > boundary
        assert result.summarized_messages == result.boundary - boundary
        assert (
            "[... 较早的消息因摘要输入预算已省略 ...]"
            not in provider.calls[step][1]["content"]
        )
        boundary = result.boundary

    assert boundary == len(messages)
    assert manager.load().summary_boundary == len(messages)
    assert len(provider.calls) == 6


def test_provider_switch_updates_inner_context_compactor(
    tmp_path: Path,
) -> None:
    """`session_compactor.provider = new` 必须同步内部 ContextCompactor。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn(1))
    old_provider = RecordingSummaryProvider(["旧 Provider 摘要。"])
    compactor = SessionCompactor(old_provider, manager)
    assert compactor.provider is old_provider
    assert compactor.compactor.provider is old_provider

    new_provider = RecordingSummaryProvider(["新 Provider 摘要。"])
    compactor.provider = new_provider

    assert compactor.provider is new_provider
    assert compactor.compactor.provider is new_provider
    result = asyncio.run(compactor.compact())
    assert result.status == "compacted"
    assert result.summary == "新 Provider 摘要。"
    assert old_provider.calls == []
    assert len(new_provider.calls) == 1


# ---------------------------------------------------------------------------
# 并发 busy
# ---------------------------------------------------------------------------


def test_concurrent_compact_returns_busy(tmp_path: Path) -> None:
    """同一 Session 的第二个并发 compact 立即返回 busy，不重复调用 LLM。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn(1))
    provider = BlockingSummaryProvider()
    first_compactor = SessionCompactor(provider, manager)
    second_compactor = SessionCompactor(provider, manager)

    async def scenario() -> None:
        first_task = asyncio.create_task(first_compactor.compact())
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        # 不同 SessionCompactor 实例共享模块级 per-session 锁。
        busy = await second_compactor.compact()
        assert busy.ok is False
        assert busy.status == "busy"
        assert busy.error == "当前 Session 正在压缩"
        assert provider.calls == 1
        provider.release.set()
        first_result = await asyncio.wait_for(first_task, timeout=2)
        assert first_result.status == "compacted"

    asyncio.run(scenario())
    assert manager.load().summary == "并发摘要：P0-1 已修复。"
    assert provider.calls == 1


def test_pending_archive_is_retried_without_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档失败后下次 compact 只补归档，不重新摘要、不调用 LLM。"""
    manager = make_manager(tmp_path)
    store = MemoryStore(manager.workspace)
    messages = [*make_complete_turn(1)]
    save_messages(manager, messages)
    provider = RecordingSummaryProvider(["先保存摘要，随后补归档。"])
    compactor = SessionCompactor(provider, manager, memory_store=store)

    original_append = store.append_history

    def failing_append(*args: Any, **kwargs: Any) -> int:
        raise MemoryStoreError("disk full (simulated)")

    monkeypatch.setattr(store, "append_history", failing_append)
    first = asyncio.run(compactor.compact())
    assert first.status == "compacted"
    assert first.archive_cursor is None
    assert store.read_history() == []
    assert compactor.has_unarchived_history(manager.load()) is True
    calls_after_first = len(provider.calls)

    monkeypatch.setattr(store, "append_history", original_append)
    second = asyncio.run(compactor.compact())
    assert second.status == "nothing_to_compact"
    assert second.archive_cursor == 1
    assert len(provider.calls) == calls_after_first
    assert compactor.has_unarchived_history(manager.load()) is False
    assert [record.content for record in store.read_history()] == [first.summary]


def test_pending_archive_is_preserved_when_new_turn_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新回合不能让归档失败的旧 checkpoint 被新版 summary 覆盖。"""
    manager = make_manager(tmp_path)
    store = MemoryStore(manager.workspace)
    save_messages(manager, make_complete_turn(1))
    provider = RecordingSummaryProvider(["旧检查点。", "合并新回合后的检查点。"])
    compactor = SessionCompactor(provider, manager, memory_store=store)
    original_append = store.append_history

    def failing_append(*args: Any, **kwargs: Any) -> int:
        raise MemoryStoreError("disk full")

    monkeypatch.setattr(store, "append_history", failing_append)
    first = asyncio.run(compactor.compact())
    session = manager.load()
    session.messages.extend(make_complete_turn(2))
    manager.save(session)
    before = manager.path.read_bytes()

    failed_retry = asyncio.run(compactor.compact())
    assert failed_retry.status == "failed"
    assert manager.path.read_bytes() == before
    assert len(provider.calls) == 1

    monkeypatch.setattr(store, "append_history", original_append)
    second = asyncio.run(compactor.compact())
    assert second.status == "compacted"
    assert [record.checkpoint_id for record in store.read_history()] == [
        first.checkpoint_id, second.checkpoint_id,
    ]
    assert len(provider.calls) == 2


@pytest.mark.parametrize("with_tools", [False, True])
def test_abandoned_turn_does_not_block_later_history(
    tmp_path: Path, with_tools: bool,
) -> None:
    """历史失败轮次按未完成事实摘要；原消息与最新未完成轮次保持不变。"""
    manager = make_manager(tmp_path)
    abandoned = make_tool_turn(2)[:-1] if with_tools else [
        {"role": "user", "content": "旧请求中断，没有最终结论。"},
    ]
    latest = make_tool_turn(4)[:-1]
    archived = [*make_complete_turn(1), *abandoned, *make_complete_turn(3)]
    messages = [*archived, *latest]
    save_messages(manager, messages)
    provider = RecordingSummaryProvider(["旧轮次中断，第三轮已回答。"])

    result = asyncio.run(SessionCompactor(provider, manager).compact())

    assert result.status == "compacted"
    assert result.boundary == len(archived)
    restored = manager.load()
    assert restored.messages == messages
    assert restored.messages[result.boundary:] == latest
    assert "此历史轮次未产生最终回答" in provider.calls[0][1]["content"]
    assert_tool_protocol(restored.messages)
