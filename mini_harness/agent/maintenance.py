"""空闲 Session 的后台压缩调度（Idle Compaction）。

``IdleCompactionScheduler`` 周期性检查当前 Session 文件是否已空闲超过阈值；
只有 Session 存在、不在活跃集合、文件 mtime 足够旧且仍有完整未归档历史时，
才在“执行前再次复查空闲状态”后调用 :class:`SessionCompactor`。任何一步
不满足都返回 ``None``，绝不调用 LLM。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Collection
from datetime import datetime
from typing import Any

from mini_harness.session.manager import SessionManager

from .session_compaction import PersistentCompactionResult, SessionCompactor

logger = logging.getLogger(__name__)


class IdleCompactionScheduler:
    """按 Session 文件空闲时间触发持久化压缩的后台调度器。

    Args:
        compactor: 已配置好的 ``SessionCompactor``。
        session_manager: 当前 Session 的 Manager；``last_active_time()``
            提供文件 mtime。
        idle_after_minutes: 空闲多少分钟后允许压缩；``<= 0`` 表示禁用。
        check_interval_seconds: 后台检查间隔；``<= 0`` 表示禁用。
        clock: 注入的时间源（秒级时间戳），默认 ``time.time``，便于测试。

    上下游:
        CLI 在 Session 模式下 :meth:`start`，每回合执行期间调用
        :meth:`mark_active` / :meth:`mark_inactive` 避免空闲压缩打断活跃
        请求；退出时 :meth:`stop`。
    """

    def __init__(
        self,
        compactor: SessionCompactor,
        session_manager: SessionManager,
        *,
        idle_after_minutes: float = 15,
        check_interval_seconds: float = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.compactor = compactor
        self.session_manager = session_manager
        self.idle_after_minutes = float(idle_after_minutes)
        self.check_interval_seconds = float(check_interval_seconds)
        self.clock = clock
        self._active_sessions: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._check_lock = asyncio.Lock()
        # 失败退避：同一 Session 在“内容未变化”时不要每个检查周期都重试
        # LLM。key -> (mtime, summary_boundary, summary_hash)；失败次数用于
        # 指数退避，成功或 Session 内容变化后清零。
        self._failure_fingerprints: dict[str, tuple[float, int, str]] = {}
        self._failure_counts: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """空闲阈值与检查间隔都为正数时才启用。"""
        return (
            self.idle_after_minutes > 0
            and self.check_interval_seconds > 0
        )

    @property
    def active_sessions(self) -> set[str]:
        """当前被标记为活跃的 Session Key 副本。"""
        return set(self._active_sessions)

    def mark_active(self, session_key: str) -> None:
        """标记 Session 正在执行，空闲压缩不得介入。"""
        if isinstance(session_key, str) and session_key:
            self._active_sessions.add(session_key)

    def mark_inactive(self, session_key: str) -> None:
        """执行结束后解除活跃标记。"""
        self._active_sessions.discard(session_key)

    @staticmethod
    def _session_fingerprint(
        session: Any,
        last_active: float,
    ) -> tuple[float, int, str]:
        """生成判断 Session 是否变化的轻量指纹。"""
        summary = getattr(session, "summary", "") or ""
        summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
        boundary = getattr(session, "summary_boundary", 0)
        if isinstance(boundary, bool) or not isinstance(boundary, int):
            boundary = 0
        return (round(float(last_active), 6), boundary, summary_hash)

    def _should_skip_retry(
        self,
        session_key: str,
        fingerprint: tuple[float, int, str],
        now: float,
    ) -> bool:
        """失败后同一 Session 内容未变化且仍在退避期时跳过。"""
        recorded = self._failure_fingerprints.get(session_key)
        if recorded is None:
            return False
        if recorded != fingerprint:
            # 新回合/Session 保存后 mtime 与 summary 都会变化，允许立即重试。
            self._clear_failure(session_key)
            return False
        retry_at = self._retry_after.get(session_key, 0.0)
        return now < retry_at

    def _record_failure(
        self,
        session_key: str,
        fingerprint: tuple[float, int, str],
        now: float,
    ) -> None:
        """记录失败并设置指数退避；避免后台每个周期重复调用 LLM。"""
        count = self._failure_counts.get(session_key, 0) + 1
        base = max(60.0, float(self.check_interval_seconds))
        delay = min(base * (2 ** min(count - 1, 6)), 3600.0)
        self._failure_fingerprints[session_key] = fingerprint
        self._failure_counts[session_key] = count
        self._retry_after[session_key] = now + delay

    def _clear_failure(self, session_key: str) -> None:
        self._failure_fingerprints.pop(session_key, None)
        self._failure_counts.pop(session_key, None)
        self._retry_after.pop(session_key, None)

    def _last_active_seconds(self) -> float | None:
        """返回 Session 文件 mtime 的时间戳；文件不存在/读取失败返回 None。"""
        try:
            last_active = self.session_manager.last_active_time()
        except Exception:  # noqa: BLE001 - 状态读取失败按“不可压缩”处理
            logger.warning("读取 Session 空闲时间失败", exc_info=True)
            return None
        if last_active is None:
            return None
        if isinstance(last_active, datetime):
            try:
                return last_active.timestamp()
            except (OSError, OverflowError, ValueError):  # pragma: no cover
                return None
        try:
            return float(last_active)
        except (TypeError, ValueError):  # pragma: no cover - 自定义返回值兼容
            return None

    def is_idle(self, *, now: float | None = None) -> bool:
        """Session 文件是否已空闲超过阈值。

        Args:
            now: 可选当前时间戳（秒）；None 时使用注入的 ``clock``。

        没有 Session 文件、读取失败或调度器被禁用时返回 False。
        """
        if not self.enabled:
            return False
        last_active = self._last_active_seconds()
        if last_active is None:
            return False
        current = self.clock() if now is None else now
        idle_seconds = self.idle_after_minutes * 60.0
        return (current - last_active) >= idle_seconds

    # ------------------------------------------------------------------
    # 检查与调度
    # ------------------------------------------------------------------

    async def wait_for_check_completion(self) -> None:
        """等待正在进行的空闲检查/压缩完成。

        CLI 在开始新回合前先 ``mark_active``，再调用本方法：

        - 若空闲检查已持锁（可能正在调用 LLM 生成摘要），等待它完成，
          避免压缩基于旧 Session 快照保存并覆盖新回合；
        - 若检查尚未开始，``mark_active`` 会让它直接跳过。
        """
        if self._check_lock.locked():
            async with self._check_lock:
                pass

    async def check_once(
        self,
        *,
        active_session_keys: Collection[str] | None = None,
    ) -> PersistentCompactionResult | None:
        """执行一次空闲检查；不满足条件时返回 None。

        顺序：Session 存在 → 不在活跃集合 → 文件 mtime 超过阈值 → 存在
        完整未归档历史 → **执行前再次检查空闲状态** → 调用 Compactor。

        Args:
            active_session_keys: 额外的活跃 Session Key 集合（例如调用方
                当前正在处理的 Session），与内部 :meth:`mark_active` 标记取并集。
        """
        if not self.enabled:
            return None
        if self._check_lock.locked():
            # 上一轮检查尚未结束：本轮直接跳过，不排队。
            return None
        async with self._check_lock:
            return await self._check_once_locked(
                active_session_keys=active_session_keys
            )

    async def _check_once_locked(
        self,
        *,
        active_session_keys: Collection[str] | None,
    ) -> PersistentCompactionResult | None:
        try:
            session = self.session_manager.load()
        except Exception:  # noqa: BLE001 - 损坏 Session 不应终止调度循环
            logger.warning("空闲检查读取 Session 失败", exc_info=True)
            return None

        last_active = self._last_active_seconds()
        if last_active is None:
            return None

        external_active = {
            str(key) for key in (active_session_keys or ())
        }
        if session.key in self._active_sessions or session.key in external_active:
            return None

        now = self.clock()
        fingerprint = self._session_fingerprint(session, last_active)
        if self._should_skip_retry(session.key, fingerprint, now):
            return None
        if not self.is_idle(now=now):
            return None

        try:
            if not self.compactor.has_unarchived_history(session):
                return None
        except Exception:  # noqa: BLE001 - 查询失败按“无新历史”处理
            logger.warning("检查未归档历史失败", exc_info=True)
            return None

        # 执行前复查：检查期间可能有新的回合写入 Session，或调用方才把
        # Session 标记为活跃；任一变化都必须放弃本次压缩。
        if session.key in self._active_sessions or session.key in external_active:
            return None
        if not self.is_idle():
            return None

        try:
            result = await self.compactor.compact(reason="idle")
        except Exception:  # noqa: BLE001 - 后台任务异常不能终止调度循环
            logger.exception("空闲压缩执行失败")
            self._record_failure(session.key, fingerprint, now)
            return None

        if result is not None and getattr(result, "status", "") == "failed":
            self._record_failure(session.key, fingerprint, now)
        elif result is not None and getattr(result, "status", "") in {
            "compacted",
            "nothing_to_compact",
        }:
            self._clear_failure(session.key)
        return result

    async def run(self) -> None:
        """调度循环：每 ``check_interval_seconds`` 调用一次 ``check_once``。"""
        if not self.enabled:
            return
        interval = self.check_interval_seconds
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=interval,
                )
                return
            except TimeoutError:
                await self._safe_check_once()

    async def _safe_check_once(self) -> None:
        """执行一次检查；意外异常只记录日志，不终止调度循环。"""
        try:
            await self.check_once()
        except Exception:  # noqa: BLE001 - 后台任务必须保持存活
            logger.exception("空闲压缩检查异常")

    def start(self) -> asyncio.Task[None] | None:
        """启动后台任务；禁用时返回 None，已在运行则返回现有 task。"""
        if not self.enabled:
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self.run(),
            name="idle-compaction-scheduler",
        )
        return self._task

    async def stop(self) -> None:
        """停止后台任务并等待其退出。"""
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


__all__ = ["IdleCompactionScheduler"]
