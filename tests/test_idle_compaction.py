"""IdleCompactionScheduler 空闲压缩调度回归测试。

覆盖：
1. Session 空闲 15 分钟后触发压缩（reason="idle"）；
2. 未达到 15 分钟、Session 不存在、无完整未归档历史时不压缩、不调 LLM；
3. 活跃 Session 不压缩，解除活跃标记后恢复；
4. idle_after_minutes=0 / check_interval_seconds=0 时完全禁用；
5. 执行前再次检查空闲状态；
6. run()/start()/stop() 后台循环按检查间隔执行；
7. 与真实 SessionCompactor 集成：空闲触发后 Session Summary 持久化。
"""

from __future__ import annotations

import asyncio
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from mini_harness.agent.maintenance import IdleCompactionScheduler
from mini_harness.agent.session_compaction import (
    PersistentCompactionResult,
    SessionCompactor,
)
from mini_harness.memory.store import MemoryStore
from mini_harness.providers.base import LLMProvider, LLMResponse
from mini_harness.session import DEFAULT_SESSION_KEY, Session, SessionManager


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def make_manager(tmp_path: Path) -> SessionManager:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return SessionManager(workspace, data_root=tmp_path / "data")


def long_text(prefix: str, repeat: int = 60) -> str:
    return prefix + "用户需求与背景说明。" * repeat


def make_complete_turn(index: int = 1) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": long_text(f"第 {index} 轮用户请求：")},
        {"role": "assistant", "content": long_text(f"第 {index} 轮最终回答：")},
    ]


def save_messages(
    manager: SessionManager,
    messages: list[dict[str, Any]],
) -> None:
    manager.save(
        Session(key=DEFAULT_SESSION_KEY, messages=deepcopy(messages))
    )


def set_session_mtime(manager: SessionManager, timestamp: float) -> None:
    os.utime(manager.path, (timestamp, timestamp))


class FakeCompactor:
    """只实现调度器依赖的两个方法的假 Compactor。"""

    def __init__(self, *, has_history: bool = True) -> None:
        self.has_history = has_history
        self.calls: list[str] = []

    def has_unarchived_history(self, session: Session | None = None) -> bool:
        return self.has_history

    async def compact(
        self,
        *,
        reason: str = "manual",
    ) -> PersistentCompactionResult:
        self.calls.append(reason)
        return PersistentCompactionResult(
            ok=True,
            status="compacted",
            reason=reason,
            summarized_messages=2,
            summary="空闲摘要。",
            boundary=2,
            checkpoint_id="cp-idle",
            archive_cursor=1,
        )


class RecordingSummaryProvider(LLMProvider):
    def __init__(self, summary: str = "空闲压缩摘要：P0-1 已修复。") -> None:
        self.summary = summary
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append(deepcopy(messages))
        return LLMResponse(content=self.summary)


# ---------------------------------------------------------------------------
# 空闲阈值
# ---------------------------------------------------------------------------


def test_idle_after_fifteen_minutes_triggers_compaction(
    tmp_path: Path,
) -> None:
    """文件 mtime 超过 15 分钟时 check_once 触发 reason='idle' 的压缩。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    now = time.time()
    set_session_mtime(manager, now - 15 * 60 - 1)
    fake = FakeCompactor()
    scheduler = IdleCompactionScheduler(
        fake,
        manager,
        idle_after_minutes=15,
        check_interval_seconds=60,
        clock=lambda: now,
    )

    assert scheduler.is_idle() is True
    result = asyncio.run(scheduler.check_once())

    assert result is not None
    assert result.status == "compacted"
    assert result.reason == "idle"
    assert fake.calls == ["idle"]


def test_not_idle_before_fifteen_minutes_does_not_compact(
    tmp_path: Path,
) -> None:
    """未达到 15 分钟阈值时返回 None，且不调用 Compactor。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    now = time.time()
    set_session_mtime(manager, now - 14 * 60)
    fake = FakeCompactor()
    scheduler = IdleCompactionScheduler(
        fake,
        manager,
        idle_after_minutes=15,
        clock=lambda: now,
    )

    assert scheduler.is_idle() is False
    assert asyncio.run(scheduler.check_once()) is None
    assert fake.calls == []


def test_no_session_file_returns_none(tmp_path: Path) -> None:
    """Session 文件不存在时返回 None，不调用 Compactor。"""
    manager = make_manager(tmp_path)
    fake = FakeCompactor()
    scheduler = IdleCompactionScheduler(fake, manager, clock=time.time)

    assert scheduler.is_idle() is False
    assert asyncio.run(scheduler.check_once()) is None
    assert fake.calls == []


def test_no_unarchived_history_returns_none(tmp_path: Path) -> None:
    """没有完整未归档历史时不压缩、不调用 LLM。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    now = time.time()
    set_session_mtime(manager, now - 20 * 60)
    fake = FakeCompactor(has_history=False)
    scheduler = IdleCompactionScheduler(
        fake,
        manager,
        idle_after_minutes=15,
        clock=lambda: now,
    )

    assert asyncio.run(scheduler.check_once()) is None
    assert fake.calls == []


# ---------------------------------------------------------------------------
# 活跃 Session
# ---------------------------------------------------------------------------


def test_active_session_is_never_compacted(tmp_path: Path) -> None:
    """mark_active 的 Session 即使空闲也不压缩；解除后可正常触发。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    now = time.time()
    set_session_mtime(manager, now - 20 * 60)
    fake = FakeCompactor()
    scheduler = IdleCompactionScheduler(
        fake,
        manager,
        idle_after_minutes=15,
        clock=lambda: now,
    )

    scheduler.mark_active(DEFAULT_SESSION_KEY)
    assert scheduler.active_sessions == {DEFAULT_SESSION_KEY}
    assert asyncio.run(scheduler.check_once()) is None
    assert fake.calls == []

    # 调用方显式传入的活跃集合同样生效。
    assert (
        asyncio.run(
            scheduler.check_once(
                active_session_keys={DEFAULT_SESSION_KEY}
            )
        )
        is None
    )
    assert fake.calls == []

    scheduler.mark_inactive(DEFAULT_SESSION_KEY)
    assert scheduler.active_sessions == set()
    result = asyncio.run(scheduler.check_once())
    assert result is not None
    assert result.status == "compacted"
    assert fake.calls == ["idle"]


def test_recheck_idle_before_execution(tmp_path: Path) -> None:
    """首次检查空闲、但执行前复查发现不再空闲时放弃压缩。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    now = time.time()
    set_session_mtime(manager, now - 20 * 60)
    fake = FakeCompactor()
    scheduler = IdleCompactionScheduler(
        fake,
        manager,
        idle_after_minutes=15,
        clock=lambda: now,
    )

    idle_results = iter([True, False])
    scheduler.is_idle = lambda *, now=None: next(idle_results)  # type: ignore[method-assign]

    assert asyncio.run(scheduler.check_once()) is None
    assert fake.calls == []


# ---------------------------------------------------------------------------
# 禁用配置
# ---------------------------------------------------------------------------


def test_zero_idle_minutes_disables_scheduler(tmp_path: Path) -> None:
    """idle_after_minutes=0 时完全禁用。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    fake = FakeCompactor()
    scheduler = IdleCompactionScheduler(
        fake,
        manager,
        idle_after_minutes=0,
        check_interval_seconds=60,
    )

    assert scheduler.enabled is False
    assert scheduler.is_idle() is False
    assert scheduler.start() is None
    assert asyncio.run(scheduler.check_once()) is None
    assert fake.calls == []


def test_zero_check_interval_disables_scheduler(tmp_path: Path) -> None:
    """check_interval_seconds=0 时后台循环不启动。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    fake = FakeCompactor()
    scheduler = IdleCompactionScheduler(
        fake,
        manager,
        idle_after_minutes=15,
        check_interval_seconds=0,
    )

    assert scheduler.enabled is False
    assert scheduler.start() is None
    asyncio.run(scheduler.run())
    assert fake.calls == []


# ---------------------------------------------------------------------------
# 后台循环
# ---------------------------------------------------------------------------


def test_start_and_stop_run_check_periodically(tmp_path: Path) -> None:
    """start() 创建后台任务，run() 按 check_interval 周期调用 check_once。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    now = time.time()
    set_session_mtime(manager, now - 20 * 60)
    fake = FakeCompactor()

    async def scenario() -> None:
        scheduler = IdleCompactionScheduler(
            fake,
            manager,
            idle_after_minutes=15,
            check_interval_seconds=0.01,
            clock=lambda: now,
        )
        task = scheduler.start()
        assert task is not None
        await asyncio.sleep(0.06)
        await scheduler.stop()
        assert task.done()

    asyncio.run(scenario())
    assert len(fake.calls) >= 1
    assert set(fake.calls) == {"idle"}


# ---------------------------------------------------------------------------
# 与真实 SessionCompactor 集成
# ---------------------------------------------------------------------------


def test_idle_compaction_persists_session_summary(
    tmp_path: Path,
) -> None:
    """空闲触发真实压缩后，Session Summary 与 Memory Archive 都持久化。"""
    manager = make_manager(tmp_path)
    store = MemoryStore(manager.workspace)
    messages = make_complete_turn()
    save_messages(manager, messages)
    now = time.time()
    set_session_mtime(manager, now - 16 * 60)
    provider = RecordingSummaryProvider("空闲压缩摘要：P0-1 已修复。")
    compactor = SessionCompactor(provider, manager, memory_store=store)
    scheduler = IdleCompactionScheduler(
        compactor,
        manager,
        idle_after_minutes=15,
        clock=lambda: now,
    )

    result = asyncio.run(scheduler.check_once())

    assert result is not None
    assert result.ok is True
    assert result.status == "compacted"
    assert result.reason == "idle"
    assert len(provider.calls) == 1
    restored = manager.load()
    assert restored.summary == "空闲压缩摘要：P0-1 已修复。"
    assert restored.summary_boundary == len(messages)
    assert restored.messages == messages
    assert [record.content for record in store.read_history()] == [
        "空闲压缩摘要：P0-1 已修复。"
    ]


def test_idle_skips_active_session_with_real_compactor(
    tmp_path: Path,
) -> None:
    """活跃 Session 即使满足空闲阈值也不调用真实 LLM。"""
    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    now = time.time()
    set_session_mtime(manager, now - 30 * 60)
    provider = RecordingSummaryProvider()
    compactor = SessionCompactor(provider, manager)
    scheduler = IdleCompactionScheduler(
        compactor,
        manager,
        idle_after_minutes=15,
        clock=lambda: now,
    )
    scheduler.mark_active(DEFAULT_SESSION_KEY)

    assert asyncio.run(scheduler.check_once()) is None
    assert provider.calls == []
    assert manager.load().summary == ""


async def test_wait_for_check_completion_waits_for_inflight_compaction(
    tmp_path: Path,
) -> None:
    """新回合开始前必须等待已经持锁的空闲压缩结束。"""
    manager = make_manager(tmp_path)
    scheduler = IdleCompactionScheduler(
        compactor=object(),  # 本测试只验证检查锁，不会调用 compact
        session_manager=manager,
        idle_after_minutes=15,
    )
    await scheduler._check_lock.acquire()
    try:
        waiter = asyncio.create_task(scheduler.wait_for_check_completion())
        await asyncio.sleep(0.01)
        assert not waiter.done(), "存在进行中的检查时必须等待"
    finally:
        scheduler._check_lock.release()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert waiter.done()


def test_idle_failure_backoff_avoids_llm_retry_storm(tmp_path: Path) -> None:
    """同一 Session 内容未变化时，失败后必须退避而不是每轮都调 LLM。"""
    class FailingCompactor(FakeCompactor):
        async def compact(self, *, reason: str = "manual") -> PersistentCompactionResult:
            self.calls.append(reason)
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                error="摘要未减少上下文 Token，已放弃本次 Compact",
            )

    manager = make_manager(tmp_path)
    save_messages(manager, make_complete_turn())
    now = time.time()
    set_session_mtime(manager, now - 30 * 60)
    compactor = FailingCompactor()
    scheduler = IdleCompactionScheduler(
        compactor,  # type: ignore[arg-type]
        manager,
        idle_after_minutes=15,
        check_interval_seconds=60,
        clock=lambda: now,
    )

    first = asyncio.run(scheduler.check_once())
    second = asyncio.run(scheduler.check_once())
    third = asyncio.run(scheduler.check_once())

    assert first is not None and first.status == "failed"
    assert second is None
    assert third is None
    assert compactor.calls == ["idle"]
