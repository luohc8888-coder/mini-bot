"""CLI 中持久化压缩和 Dream 的组装与生命周期。

MemoryMaintenanceMixin 使用 InteractiveCLI 组装的会话、记忆和配置状态，
统一处理手动维护命令与后台任务，不复制 Session。
"""

from __future__ import annotations

import asyncio
from mini_harness.providers.base import LLMProvider


class MemoryMaintenanceMixin:
    """同一 CLI 实例上的维护组件与任务生命周期。"""

    def _ensure_memory_runtime(self, provider: LLMProvider | None) -> None:
        """按当前 Provider 创建/更新持久化压缩与 Dream 运行态。"""
        if provider is not None:
            self._active_provider = provider
        if self.memory_store is None:
            return
        try:
            from mini_harness.agent.session_compaction import SessionCompactor
            from mini_harness.memory.dream import DreamConfig, DreamManager
        except Exception:  # noqa: BLE001 - 新增模块不可用时保持基础对话
            return

        if self._session_compactor is None:
            self._session_compactor = SessionCompactor(
                self._active_provider,
                self.session_manager,
                memory_store=self.memory_store,
                context_config=self.context_config,
            )
        else:
            if self._active_provider is not None:
                self._session_compactor.provider = self._active_provider
            if getattr(self._session_compactor, "context_config", None) is not None:
                self._session_compactor.context_config = self.context_config

        if self._dream_manager is None:
            self._dream_manager = DreamManager(
                self._active_provider,
                self.memory_store,
                DreamConfig(
                    enabled=(
                        self.context_config.dream_enabled
                        if self._dream_enabled is None
                        else self._dream_enabled
                    ),
                    interval_hours=(
                        self.context_config.dream_interval_hours
                        if self._dream_interval_hours is None
                        else self._dream_interval_hours
                    ),
                    max_entries=self.context_config.dream_max_entries,
                ),
                context_config=self.context_config,
            )
        elif self._active_provider is not None:
            self._dream_manager.provider = self._active_provider
            self._dream_manager.context_config = self.context_config

    def _bind_memory_provider(self, provider: LLMProvider) -> None:
        """Provider 切换后同步后台压缩与 Dream，避免使用旧连接。"""
        self._ensure_memory_runtime(provider)

    def _start_maintenance(self) -> None:
        """启动 Idle Compaction 与 Dream 后台任务（同一个 CLI 生命周期）。"""
        if (
            not self._maintenance_enabled
            or self.memory_store is None
        ):
            return
        if self._maintenance_tasks:
            return
        self._ensure_memory_runtime(self._active_provider)
        try:
            from mini_harness.agent.maintenance import IdleCompactionScheduler
            from mini_harness.memory.scheduler import DreamScheduler
        except Exception:  # noqa: BLE001 - 后台维护不可用不能阻止聊天
            return

        config = self.context_config
        idle_after_minutes = (
            config.idle_compact_after_minutes
            if self._idle_compact_after_minutes is None
            else self._idle_compact_after_minutes
        )
        dream_enabled = (
            config.dream_enabled
            if self._dream_enabled is None
            else self._dream_enabled
        )
        dream_interval_hours = (
            config.dream_interval_hours
            if self._dream_interval_hours is None
            else self._dream_interval_hours
        )
        if (
            self._session_compactor is not None
            and idle_after_minutes > 0
            and config.idle_compact_check_interval_seconds > 0
        ):
            self._idle_scheduler = IdleCompactionScheduler(
                self._session_compactor,
                self.session_manager,
                idle_after_minutes=idle_after_minutes,
                check_interval_seconds=config.idle_compact_check_interval_seconds,
            )
            task = self._idle_scheduler.start()
            if task is not None:
                self._maintenance_tasks.append(task)

        if self._dream_manager is not None and dream_enabled:
            self._dream_scheduler = DreamScheduler(
                self._dream_manager,
                interval_hours=dream_interval_hours,
                run_on_start=True,
            )
            task = self._dream_scheduler.start()
            if task is not None:
                self._maintenance_tasks.append(task)

    async def _stop_maintenance(self) -> None:
        """停止后台维护任务，确保 CLI 退出时没有悬挂协程。"""
        for scheduler in (self._idle_scheduler, self._dream_scheduler):
            if scheduler is not None:
                try:
                    await scheduler.stop()
                except Exception:  # noqa: BLE001 - 退出清理不应掩盖主错误
                    pass
        tasks = [task for task in self._maintenance_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._maintenance_tasks.clear()
        self._idle_scheduler = None
        self._dream_scheduler = None

    async def _handle_compact_command(self) -> None:
        """处理 ``/compact``：不依赖 Token 阈值与空闲时间。"""
        if self.memory_store is None:
            self.console.write("当前没有可压缩的 Session。")
            return
        self._ensure_memory_runtime(self._active_provider)
        if self._session_compactor is None:
            self.console.write("压缩功能不可用：SessionCompactor 未初始化。")
            return
        try:
            result = await self._session_compactor.compact(reason="manual")
        except Exception as exc:  # noqa: BLE001 - 命令错误不能打崩 CLI
            self.console.write_error(f"压缩失败：{type(exc).__name__}: {exc}")
            return
        status = getattr(result, "status", "failed")
        if status == "compacted":
            message = (
                f"压缩成功：已归档 {result.summarized_messages} 条历史，"
                f"Summary 已保存（边界 {result.boundary}，"
                f"checkpoint {result.checkpoint_id or '无'}）。"
            )
            if getattr(result, "archive_cursor", None) is not None:
                message += f" Memory Archive cursor={result.archive_cursor}。"
            if getattr(result, "error", None):
                message += f" 归档提示：{result.error}"
            self.console.write(message)
            return
        if status == "nothing_to_compact":
            if getattr(result, "archive_cursor", None) is not None:
                self.console.write(
                    "没有新的未归档历史；已补归档 Summary 到 Memory Archive"
                    f"（cursor={result.archive_cursor}）。"
                )
            else:
                self.console.write("没有新的未归档历史，无需压缩。")
            return
        if status == "busy":
            self.console.write("当前 Session 正在压缩，请稍后再试。")
            return
        self.console.write_error(
            f"压缩失败：{getattr(result, 'error', None) or '未知原因'}"
        )

    async def _handle_dream_command(self) -> None:
        """处理 ``/dream``：手动触发长期记忆整理。"""
        if self.memory_store is None:
            self.console.write("当前没有可用的长期记忆存储。")
            return
        self._ensure_memory_runtime(self._active_provider)
        if self._dream_manager is None:
            self.console.write("Dream 不可用：DreamManager 未初始化。")
            return
        try:
            result = await self._dream_manager.run()
        except Exception as exc:  # noqa: BLE001 - 命令错误不能打崩 CLI
            self.console.write_error(f"Dream 失败：{type(exc).__name__}: {exc}")
            return
        if getattr(result, "busy", False):
            self.console.write("Dream 正在运行，请稍后再试。")
            return
        if getattr(result, "error", None):
            self.console.write_error(f"Dream 失败：{result.error}")
            return
        if result.processed_entries <= 0:
            self.console.write("没有待处理的归档摘要，Dream 已跳过（未调用模型）。")
            return
        self.console.write(
            f"Dream 完成：处理 {result.processed_entries} 条归档摘要，"
            f"新增 {result.added} 条、更新 {result.updated} 条、"
            f"删除 {result.removed} 条、跳过 {result.skipped} 条；"
            f"cursor={result.cursor}，"
            f"MEMORY.md {'已更新' if result.memory_updated else '无变化'}。"
        )
