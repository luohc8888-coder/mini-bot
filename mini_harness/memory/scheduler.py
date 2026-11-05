"""Dream 后台调度生命周期。"""

from __future__ import annotations

import asyncio
import logging
from .dream import DreamManager, DreamResult

logger = logging.getLogger(__name__)


class DreamScheduler:
    """后台定时 Dream 调度器。

    Args:
        dream: DreamManager 实例。
        interval_hours: 两次 Dream 之间的间隔小时数。
        run_on_start: 启动时是否立即执行一次 Dream。

    上下游:
        CLI 在 Session 模式下调用 :meth:`start` 创建后台任务，退出时调用
        :meth:`stop` 取消。``DreamConfig.enabled=False`` 时不启动。
    """

    def __init__(
        self,
        dream: DreamManager,
        *,
        interval_hours: float = 2.0,
        run_on_start: bool = True,
    ) -> None:
        self.dream = dream
        self.interval_hours = interval_hours
        self.run_on_start = run_on_start
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def _enabled(self) -> bool:
        return bool(getattr(self.dream.config, "enabled", True))

    async def run_once(self) -> DreamResult | None:
        """执行一次 Dream；禁用时返回 None。"""
        if not self._enabled():
            return None
        return await self.dream.run()

    async def run(self) -> None:
        """调度循环：首次立即检查（可选），之后每 interval 执行一次。"""
        if not self._enabled():
            return

        if self.run_on_start:
            await self._safe_run_once()

        interval_seconds = float(self.interval_hours) * 3600.0
        if interval_seconds <= 0:
            return

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                await self._safe_run_once()

    async def _safe_run_once(self) -> None:
        """执行一次 Dream；意外异常只记录日志，不终止调度循环。"""
        try:
            await self.run_once()
        except Exception:  # noqa: BLE001 - 后台任务必须保持存活
            logger.exception("Dream 定时任务执行异常")

    def start(self) -> asyncio.Task[None] | None:
        """启动后台任务；已运行则返回现有 task，禁用时返回 None。"""
        if not self._enabled():
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self.run(), name="dream-scheduler")
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
