"""Dream 长期记忆引擎：从会话归档提取结构化记忆操作并维护 MEMORY.md。

Dream 是记忆系统的“慢路径”：``SessionCompactor`` 把已完成对话压缩成归档
摘要写入 ``history.jsonl``；Dream 定期读取尚未处理的归档，调用 LLM 提炼
长期偏好、稳定决策、架构约定、长期目标与经验知识，再以确定性的
add/update/remove 操作更新 ``MEMORY.md``。

设计约束（见 docs/memory_compaction_design.md §5）：

- LLM 只返回结构化操作，绝不直接接受 LLM 返回的整份 MEMORY.md；
- 文本规范化、section 归类、去重、大小限制全部由本模块确定性完成；
- 只有 MEMORY.md 原子写入成功（或无需修改）后才推进 ``.dream_cursor``；
- Provider 异常、空响应、非法 JSON / 结构、写入失败时 cursor 不变，
  MEMORY.md 保持不变。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mini_harness.providers.base import LLMProvider

from .store import HistoryRecord, MemoryStore

if TYPE_CHECKING:
    from mini_harness.agent.context_types import ContextConfig

from .document import _MemoryDocument, _parse_operations, _apply_operations

logger = logging.getLogger(__name__)


# 已有 Dream 运行时，后续调用立即返回该错误，不排队也不调用 LLM。
BUSY_ERROR = "Dream 正在运行"

DREAM_SYSTEM_PROMPT = """你是 Mini Harness 的长期记忆整理器（Dream）。你会收到“当前 MEMORY.md”和“新增的会话归档摘要”，需要从中提炼值得长期保留的事实，并用结构化操作更新 MEMORY.md。

只输出一个 JSON 对象，不要输出分析过程、Markdown 标题或其他解释文字（可以用 ```json 围栏包裹）。JSON 结构必须是：
{
  "add": [{"section": "用户偏好", "text": "一条原子事实"}],
  "update": [{"old": "已有条目", "new": "更新后的条目"}],
  "remove": ["已有条目"]
}

字段规则：
- 三个字段都必须是数组：add 的元素是 {"section": string, "text": string}，update 的元素是 {"old": string, "new": string}，remove 的元素是 string。
- add.section 只能是以下之一："用户偏好"、"项目决策"、"架构约定"、"长期目标"、"经验知识"、"其他"。
- add.text 必须是一条独立、完整、可长期复用的原子事实；使用简洁中文，并保留精确的路径、函数名、版本号等标识符。
- update.old 与 remove 中的字符串必须与 MEMORY.md 已有条目一致（比较时会忽略首尾空白、连续空白和 bullet 前缀）；不要引用不存在的条目。
- update 用于修正、合并或更新已有条目；remove 只用于删除已经失效、被明确否定或按要求遗忘的条目。
- add/update/remove 都可以是空数组；即使没有操作也要输出完整 JSON。

提取规则（只保留长期价值）：
- 提取：长期偏好、稳定的技术决策、架构约定、未完成的长期目标、经过确认的经验知识。
- 排除：闲聊、一次性问题、临时日志、完整工具输出、推测或未确认的猜测、重复记忆。
- 如果新增归档中没有值得长期保存的内容，只输出 (nothing)，不要输出 JSON。

输出示例：
```json
{"add": [{"section": "项目决策", "text": "项目使用 Python 3.14 与 pytest 作为测试框架"}], "update": [], "remove": []}
```
"""


@dataclass(slots=True)
class DreamConfig:
    """Dream 引擎配置。

    Args:
        enabled: 是否允许后台定时 Dream；``/dream`` 手动触发不受此限制。
        interval_hours: DreamScheduler 的检查间隔小时数。
        max_entries: 单次 Dream 最多处理的 history 归档条数。
        max_memory_bytes: MEMORY.md 的最大字节数（UTF-8），超限整次失败。
    """

    enabled: bool = True
    interval_hours: float = 2.0
    max_entries: int = 20
    max_memory_bytes: int = 64 * 1024


@dataclass(slots=True)
class DreamResult:
    """一次 Dream 运行的结果。

    Attributes:
        processed_entries: 本次实际处理的 history 记录数。
        added: 成功新增的条目数。
        updated: 成功更新的条目数。
        removed: 成功删除的条目数。
        skipped: 因非法、重复或找不到目标而跳过的操作数。
        cursor: 运行结束后的 Dream cursor（失败时保持旧值）。
        memory_updated: 是否实际改写了 MEMORY.md。
        llm_called: 是否真实调用了 Provider（无新记录时恒为 False）。
        busy: 是否因已有 Dream 运行而直接返回。
        error: 失败原因；成功时为 None。
    """

    processed_entries: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    cursor: int = 0
    memory_updated: bool = False
    llm_called: bool = False
    busy: bool = False
    error: str | None = None


class DreamManager:
    """长期记忆整理引擎。

    Args:
        provider: 用于提炼记忆的 LLM Provider；CLI 切换模型时可直接替换
            ``self.provider``。
        store: MemoryStore，负责 history / MEMORY.md / cursor 的文件 I/O。
        config: DreamConfig；None 时使用默认配置。
        context_config: 请求预算；None 按 Provider 能力推导，未知能力使用默认预算。

    上下游:
        SessionCompactor 通过 ``MemoryStore.append_history`` 写入归档；
        DreamManager 读取未处理归档并维护 MEMORY.md；DreamScheduler 与
        CLI ``/dream`` 都调用 :meth:`run`。
    """

    def __init__(
        self,
        provider: LLMProvider,
        store: MemoryStore,
        config: DreamConfig | None = None,
        *,
        context_config: ContextConfig | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.config = config if config is not None else DreamConfig()
        self.context_config = context_config
        # 实例级互斥：/dream 手动触发与后台定时任务共用同一把锁。
        self._lock = asyncio.Lock()

    async def run(self, *, max_entries: int | None = None) -> DreamResult:
        """执行一次 Dream。

        Args:
            max_entries: 覆盖本次最多处理的 history 条数；None 使用配置值。

        Returns:
            DreamResult；失败时 ``error`` 非空且 cursor / MEMORY.md 不变。
        """
        if self._lock.locked():
            return DreamResult(busy=True, error=BUSY_ERROR)

        async with self._lock:
            return await self._run_locked(max_entries=max_entries)

    async def _run_locked(self, *, max_entries: int | None) -> DreamResult:
        """持锁执行 Dream 的完整流程。"""
        try:
            current_cursor = self.store.get_dream_cursor()
        except Exception as exc:  # noqa: BLE001
            return DreamResult(
                processed_entries=0,
                cursor=0,
                error=f"读取 Dream cursor 失败：{exc}",
            )
        # 防御 history.jsonl 被重建/迁移后 cursor 大于最新记录：否则
        # read_history(since_cursor=陈旧 cursor) 会永远返回空，Dream 静默
        # 停止工作。无法判断旧 cursor 对应哪些记录，回到 0 重放现有归档
        # （MEMORY.md 的确定性去重保证不会产生重复条目）。
        try:
            latest_cursor = self.store.latest_cursor()
        except Exception as exc:  # noqa: BLE001
            return DreamResult(
                processed_entries=0,
                cursor=current_cursor,
                error=f"读取归档历史失败：{exc}",
            )
        read_cursor = current_cursor
        if current_cursor > latest_cursor:
            logger.warning(
                "Dream cursor %s 超过 history 最新 cursor %s，回退到 0 重放",
                current_cursor,
                latest_cursor,
            )
            # 只调整本次读取起点；成功处理前不改持久化 cursor。
            read_cursor = 0
        effective_max = (
            self.config.max_entries if max_entries is None else max_entries
        )
        if effective_max <= 0:
            return DreamResult(processed_entries=0, cursor=current_cursor)

        try:
            batch = self.store.read_history(
                since_cursor=read_cursor, limit=effective_max
            )
        except Exception as exc:  # noqa: BLE001 - 统一转换为 DreamResult 错误
            return DreamResult(
                processed_entries=0,
                cursor=current_cursor,
                error=f"读取归档历史失败：{exc}",
            )

        # 防御性截断：即使 store 忽略 limit，也不超过本次 max_entries。
        batch = batch[:effective_max]
        if not batch:
            # 无未处理记录时绝不调用 LLM。
            return DreamResult(processed_entries=0, cursor=current_cursor)

        try:
            current_memory = self.store.read_memory()
        except Exception as exc:  # noqa: BLE001
            return DreamResult(
                processed_entries=len(batch),
                cursor=current_cursor,
                error=f"读取 MEMORY.md 失败：{exc}",
            )

        try:
            # 切换模型只影响下一次 Dream，不改变正在执行的请求及其预算。
            provider = self.provider
            input_budget, output_reserve = self._request_limits(provider)
            batch, requests = self._build_requests(current_memory, batch, input_budget)
        except ValueError as exc:
            return DreamResult(
                cursor=current_cursor,
                error=str(exc),
            )

        document = _MemoryDocument.parse(current_memory)
        added = updated = removed = skipped = 0
        from mini_harness.agent.tokens import estimate_messages_tokens

        # 超大单条归档逐片处理，不丢弃中间内容。所有片段成功后才写记忆并
        # 推进该条 cursor，任一片失败都保留旧文件，重试可完整重放。
        for messages in requests:
            output_budget = min(
                provider.max_output_tokens,
                input_budget + output_reserve - estimate_messages_tokens(messages),
            )
            try:
                with provider.request_output_budget(output_budget):
                    response = await provider.chat(messages=messages, tools=None)
            except Exception as exc:  # noqa: BLE001 - Provider 失败安全回退
                logger.warning("Dream Provider 调用失败：%s", exc)
                return DreamResult(
                    processed_entries=len(batch),
                    cursor=current_cursor,
                    llm_called=True,
                    error=f"Dream Provider 调用失败：{exc}",
                )

            content = (
                response if isinstance(response, str)
                else getattr(response, "content", None)
            )
            try:
                if getattr(response, "finish_reason", "stop") in {
                    "length", "max_tokens",
                }:
                    raise ValueError("Dream 模型输出因长度上限截断，已放弃本次操作")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Dream Provider 返回空响应")
                operations = _parse_operations(content)
            except ValueError as exc:
                return DreamResult(
                    processed_entries=len(batch),
                    cursor=current_cursor,
                    llm_called=True,
                    error=str(exc),
                )
            counts = _apply_operations(document, operations)
            added += counts[0]
            updated += counts[1]
            removed += counts[2]
            skipped += counts[3]

        # 没有任何有效修改时不重写文件，直接推进 cursor（例如 LLM 输出
        # (nothing) 或全部操作都是重复/无效条目）。
        if added or updated or removed:
            new_memory = document.render()
            if len(new_memory.encode("utf-8")) > self.config.max_memory_bytes:
                return DreamResult(
                    processed_entries=len(batch),
                    cursor=current_cursor,
                    llm_called=True,
                    error=(
                        "MEMORY.md 超过大小上限 "
                        f"{self.config.max_memory_bytes} 字节，整次操作失败"
                    ),
                )
            if new_memory != current_memory:
                try:
                    self.store.write_memory(new_memory)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Dream 写入 MEMORY.md 失败：%s", exc)
                    return DreamResult(
                        processed_entries=len(batch),
                        cursor=current_cursor,
                        llm_called=True,
                        error=f"写入 MEMORY.md 失败：{exc}",
                    )
                memory_updated = True
            else:
                memory_updated = False
        else:
            memory_updated = False

        # 只有 MEMORY.md 写入成功（或无需修改）后才推进 cursor。cursor
        # 写入失败时保持旧 cursor，下次会重放同一批记录，由去重兜底。
        new_cursor = batch[-1].cursor
        try:
            self.store.set_dream_cursor(new_cursor)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dream 推进 cursor 失败：%s", exc)
            return DreamResult(
                processed_entries=len(batch),
                cursor=current_cursor,
                added=added,
                updated=updated,
                removed=removed,
                skipped=skipped,
                memory_updated=memory_updated,
                llm_called=True,
                error=f"写入 Dream cursor 失败：{exc}",
            )

        return DreamResult(
            processed_entries=len(batch),
            added=added,
            updated=updated,
            removed=removed,
            skipped=skipped,
            cursor=new_cursor,
            memory_updated=memory_updated,
            llm_called=True,
        )

    def _request_limits(self, provider: LLMProvider | None = None) -> tuple[int, int]:
        """维护请求也遵守模型窗口、CLI 预算与输出预留。"""
        from mini_harness.agent.context_types import ContextConfig

        provider = self.provider if provider is None else provider
        config = self.context_config or ContextConfig.from_provider(provider)
        window = config.max_context_tokens
        provider_window = getattr(provider, "context_window_tokens", 0)
        if provider_window > 0:
            window = min(window, provider_window)
        reserve = max(1, config.reserve_output_tokens)
        budget = window - reserve - config.safety_margin_tokens
        if budget <= 0:
            raise ValueError("Dream 输入预算不足，请提高上下文窗口或降低输出预留")
        return budget, reserve

    def _build_requests(
        self,
        current_memory: str,
        batch: list[HistoryRecord],
        budget: int,
    ) -> tuple[list[HistoryRecord], list[list[dict[str, Any]]]]:
        """按完整归档选取连续前缀；超大首条分片，覆盖全部正文。"""
        from mini_harness.agent.tokens import estimate_messages_tokens, truncate_text_to_token_budget

        def messages(memory: str, records: list[HistoryRecord]) -> list[dict[str, Any]]:
            return [
                {"role": "system", "content": DREAM_SYSTEM_PROMPT},
                {"role": "user", "content": self._build_user_message(memory, records)},
            ]

        payload_budget = budget - estimate_messages_tokens(messages("", []))
        if payload_budget <= 0:
            raise ValueError("Dream 输入预算不足以容纳系统提示，未调用 LLM")
        # 记忆只裁剪发送副本；操作始终在完整文档上校验/去重，不删除未展示内容。
        preview = truncate_text_to_token_budget(
            current_memory, payload_budget // 3,
            marker="\n[记忆仅展示部分，完整文件仍保留]\n",
        )
        selected: list[HistoryRecord] = []
        for record in batch:
            candidate = messages(preview, [*selected, record])
            if estimate_messages_tokens(candidate) > budget:
                break
            selected.append(record)
        if selected:
            return selected, [messages(preview, selected)]

        # 首条自身过大时不能截断后推进整条 cursor，否则中间事实永久丢失。
        # 按字符二分出能放下的最大前缀；各片顺序输入同一内存文档，统一提交。
        record = batch[0]
        content = record.content
        offset = 0
        requests: list[list[dict[str, Any]]] = []
        while offset < len(content):
            prefix = f"[归档 {record.cursor} 的连续片段 {len(requests) + 1}]\n"
            low, high = 0, len(content) - offset
            while low < high:
                size = (low + high + 1) // 2
                fragment = replace(record, content=prefix + content[offset:offset + size])
                if estimate_messages_tokens(messages(preview, [fragment])) <= budget:
                    low = size
                else:
                    high = size - 1
            if low == 0:
                raise ValueError("Dream 输入预算不足以容纳归档元数据，未调用 LLM")
            fragment = replace(record, content=prefix + content[offset:offset + low])
            requests.append(messages(preview, [fragment]))
            offset += low
        if not requests:
            raise ValueError("Dream 输入预算不足以容纳归档，未调用 LLM")
        return [record], requests

    @staticmethod
    def _build_user_message(
        current_memory: str, batch: list[HistoryRecord]
    ) -> str:
        """构造 Dream 的 user message：当前记忆 + 未处理归档。"""
        memory_text = current_memory.strip() or "(空)"
        records: list[str] = []
        for record in batch:
            records.append(
                f"### 归档 {record.cursor}（{record.timestamp}，"
                f"{record.session_key}）\n{record.content.strip()}"
            )
        history_text = "\n\n".join(records)
        return (
            "以下是当前的 MEMORY.md 内容：\n\n"
            f"{memory_text}\n\n"
            "以下是上次 Dream 之后新增的会话归档摘要：\n\n"
            f"{history_text}\n\n"
            "请根据系统提示输出一个 JSON 对象（add/update/remove）；"
            "如果没有值得长期保存的内容，只输出 (nothing)。"
        )


__all__ = ["DreamConfig", "DreamManager", "DreamResult", "DREAM_SYSTEM_PROMPT", "BUSY_ERROR"]
