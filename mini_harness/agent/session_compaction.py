"""持久化 Session 压缩（Session Summary Checkpoint）。

本模块把“已完成但尚未归档”的用户轮次交给 :class:`ContextCompactor` 生成
摘要，然后把摘要正文、归档边界（``summary_boundary``）与稳定 checkpoint ID
一次性原子写入 Session，成功后把同一摘要归档到 ``MemoryStore``。

与请求内的 ``ContextGovernor`` 不同，这里的压缩结果是**跨进程持久化**的：
- ``Session.messages`` 永远保留完整原始历史，压缩只移动 ``summary_boundary``；
- 失败、非法或“摘要未减少 Token”时 Session 文件完全不变；
- 同一 Session 的并发压缩由模块级 :class:`asyncio.Lock` 字典互斥，已持锁
  时立即返回 ``busy``，不排队、不重复调用 LLM。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from mini_harness.memory.store import MemoryStore
from mini_harness.session.manager import Session, SessionManager

from .compaction import (
    SUMMARY_SYSTEM_PROMPT,
    CompactionOutcome,
    ContextCompactor,
    _content_text,
    _json_text,
    _tool_call_index,
    _tool_call_name,
    _truncate_head_tail,
)
from .context import (
    ContextConfig,
    _is_valid_turn,
    _split_history_into_turns,
    estimate_tokens,
    truncate_text_to_token_budget,
)

logger = logging.getLogger(__name__)

# 模块级 per-session 压缩锁。多个 SessionCompactor 实例（例如 CLI 手动
# /compact 与后台 Idle Compaction）指向同一 Session 文件时必须互斥。
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    """取得指定 Session 的模块级锁。

    ``asyncio.Lock`` 一旦发生等待就会绑定事件循环；测试或重连场景可能在
    不同事件循环里复用同一进程级字典，因此检测到旧锁绑定到其他循环时
    重建一把新锁，避免 ``RuntimeError``。
    """
    lock = _SESSION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[key] = lock
        return lock
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 同步上下文只读锁状态
        loop = None
    bound_loop = getattr(lock, "_loop", None)
    if bound_loop is not None and loop is not None and bound_loop is not loop:
        lock = asyncio.Lock()
        _SESSION_LOCKS[key] = lock
    return lock


@dataclass(slots=True)
class PersistentCompactionResult:
    """一次持久化 Session 压缩尝试的结果。

    Args:
        ok: 操作本身是否成功（``nothing_to_compact`` 也算成功）。
        status: ``"compacted" | "nothing_to_compact" | "failed" | "busy"``。
        reason: 触发来源，``"manual"`` 或 ``"idle"``。
        summarized_messages: 本次新归档的原始消息条数。
        summary: 持久化的纯摘要正文。
        boundary: 压缩后的 ``summary_boundary``（已被摘要覆盖的消息数）。
        checkpoint_id: 由 session key + boundary + summary 派生的稳定 ID。
        archive_cursor: Memory Archive 记录的 cursor；未归档/失败时为 None。
        error: 失败原因；Session 已保存但归档失败时也会携带归档错误。
    """

    ok: bool
    status: str
    reason: str
    summarized_messages: int = 0
    summary: str = ""
    boundary: int = 0
    checkpoint_id: str | None = None
    archive_cursor: int | None = None
    error: str | None = None


def _complete_turn_groups(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """选择从开头起连续的、以最终 assistant 回答收尾的完整用户轮次。

    规则：
    - 复用 :func:`_split_history_into_turns` 按 user 消息切分；
    - 每个轮次必须通过 :func:`_is_valid_turn`（工具调用/结果完整配对）；
    - 轮次最后一条必须是 ``assistant`` 且没有 ``tool_calls``，即模型已经
      给出最终回答；
    - 遇到第一个不完整/非法轮次立即停止，当前未完成请求永远不进入摘要。

    返回按消息顺序排列的轮次列表（每项是一轮的消息），便于调用方按完整
    轮次边界选择“可完整摘要的前缀”。
    """
    selected: list[list[dict[str, Any]]] = []
    for turn in _split_history_into_turns(messages):
        if not _is_valid_turn(turn):
            break
        last_message = turn[-1]
        if not isinstance(last_message, dict):
            break
        if (
            last_message.get("role") != "assistant"
            or last_message.get("tool_calls")
        ):
            break
        selected.append(turn)
    return selected


def _complete_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回全部完整轮次摊平后的消息列表。"""
    return [
        message
        for turn in _complete_turn_groups(messages)
        for message in turn
    ]


def _summary_transcript_max_tokens(
    summary_system_prompt: str,
    budget: int,
) -> int:
    """复刻 ``ContextCompactor._build_transcript`` 的 transcript Token 上限。"""
    prompt_overhead = estimate_tokens(summary_system_prompt) + 128
    effective_budget = max(int(budget), 0)
    return max(
        64,
        min(
            max(128, int(effective_budget * 0.55)),
            max(64, effective_budget - prompt_overhead),
        ),
    )


def _render_transcript_entry(
    message: Any,
    *,
    assistant_cap: int,
    call_index: dict[str, Any],
) -> str:
    """按 ``ContextCompactor._build_transcript`` 的规则渲染一条消息条目。

    这里必须与 ``_build_transcript`` 的截断上限保持一致：user ≤ 500 tokens、
    tool ≤ 300 tokens、assistant ≤ ``assistant_cap`` tokens，tool call 参数按
    字符截断 300；只有完全复刻这些上限，才能保证被选中的轮次真实完整地进入
    摘要输入，而不是被 ``_build_transcript`` 静默丢弃/截断。
    """
    if not isinstance(message, dict):
        return (
            "[unknown]\n"
            + truncate_text_to_token_budget(str(message), 200)
        )
    role = str(message.get("role", "unknown"))
    if role == "tool":
        call_id = str(message.get("tool_call_id") or "")
        name = str(message.get("name") or "")
        if not name and call_id in call_index:
            name = call_index[call_id].name
        content = truncate_text_to_token_budget(_content_text(message), 300)
        return f"[tool:{name or 'unknown'}]\n{content}"
    if role == "assistant":
        content = _truncate_head_tail(
            _content_text(message),
            assistant_cap,
            "\n\n[... assistant 中间内容已截断 truncated ...]\n\n",
        )
        lines = ["[assistant]", content]
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            rendered_calls = []
            for call in tool_calls:
                name = _tool_call_name(call) or "unknown"
                arguments = call.get("arguments")
                arguments_text = _json_text(arguments)
                if len(arguments_text) > 300:
                    arguments_text = arguments_text[:300] + "…"
                rendered_calls.append(f"{name}({arguments_text})")
            lines.append(
                "[assistant tool_calls] " + "; ".join(rendered_calls)
            )
        return "\n".join(lines)
    content = truncate_text_to_token_budget(
        _content_text(message),
        500 if role == "user" else 300,
    )
    return f"[{role}]\n{content}"


def _generate_checkpoint_id(
    session_key: str,
    boundary: int,
    summary: str,
) -> str:
    """由 ``session_key + boundary + summary`` 生成稳定 checkpoint ID。"""
    payload = json.dumps(
        [session_key, boundary, summary],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class SessionCompactor:
    """把已完成的未归档历史压缩为持久化 Session Summary Checkpoint。

    Args:
        provider: 当前 LLM Provider，透传给默认 ``ContextCompactor``。
        session_manager: 负责原子读写 Session 文件的 Manager。
        memory_store: 可选 Memory Archive；成功后把纯摘要写入
            ``history.jsonl``（用 checkpoint_id 去重）。
        context_config: 可选上下文配置；默认使用 ``ContextConfig()``，
            摘要预算取 ``available_input_tokens``。
        compactor: 可选 ``ContextCompactor`` 注入，便于测试；默认复用
            ``ContextCompactor(provider)``。

    上下游:
        CLI 的 ``/compact`` 与 ``IdleCompactionScheduler`` 都调用
        :meth:`compact`；本类不修改 ``ContextCompactor`` / ``ContextGovernor``
        的现有请求内压缩逻辑。
    """

    def __init__(
        self,
        provider: Any,
        session_manager: SessionManager,
        *,
        memory_store: MemoryStore | None = None,
        context_config: ContextConfig | None = None,
        compactor: ContextCompactor | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.memory_store = memory_store
        self.context_config = (
            context_config if context_config is not None else ContextConfig()
        )
        self.compactor = (
            compactor if compactor is not None else ContextCompactor(provider)
        )
        self._provider: Any = provider
        # __init__ 期间也走一遍 setter：注入的 compactor 可能仍绑定旧
        # provider，构造后必须与当前 provider 保持一致。
        self.provider = provider
        session_path = getattr(session_manager, "path", None)
        self._lock_key = (
            str(session_path)
            if session_path is not None
            else str(getattr(session_manager, "session_key", "default"))
        )

    @property
    def provider(self) -> Any:
        """当前 LLM Provider；与内部 ``ContextCompactor`` 保持同步。"""
        return self._provider

    @provider.setter
    def provider(self, value: Any) -> None:
        self._provider = value
        compactor = getattr(self, "compactor", None)
        if compactor is not None and hasattr(compactor, "provider"):
            try:
                compactor.provider = value
            except (AttributeError, TypeError):  # pragma: no cover - 只读自定义实现
                logger.debug("自定义 Compactor 的 provider 不可写，已跳过同步")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def has_unarchived_history(self, session: Session | None = None) -> bool:
        """是否存在至少一个完整、未归档的用户轮次。

        Args:
            session: 已加载的 Session；None 时从 ``session_manager`` 读取。

        Returns:
            存在完整未归档轮次为 True；Session 不存在、读取失败或只有
            不完整轮次时为 False。本方法绝不调用 LLM。
        """
        if session is None:
            try:
                session = self.session_manager.load()
            except Exception:  # noqa: BLE001 - 查询失败按“无新历史”处理
                logger.warning("读取 Session 失败，跳过压缩检查", exc_info=True)
                return False
        messages = getattr(session, "messages", None)
        if not isinstance(messages, list):
            return False
        boundary = getattr(session, "summary_boundary", 0)
        if isinstance(boundary, bool) or not isinstance(boundary, int):
            return False
        boundary = max(0, min(boundary, len(messages)))
        if _complete_turns(messages[boundary:]):
            return True
        # 上一次压缩已保存 Session Summary，但 Memory Archive 追加失败；
        # 在 Session 内容未变化前允许调度器再次触发“补归档”（不调用 LLM）。
        return self._has_pending_archive(session)

    def _has_pending_archive(self, session: Session) -> bool:
        """是否存在已保存 Summary 但尚未成功写入 history.jsonl 的检查点。"""
        if self.memory_store is None:
            return False
        summary = getattr(session, "summary", "")
        checkpoint_id = getattr(session, "summary_checkpoint_id", "")
        if not isinstance(summary, str) or not summary.strip():
            return False
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            return False
        try:
            return not self.memory_store.has_checkpoint(checkpoint_id)
        except Exception:  # noqa: BLE001 - 查询失败按“无待归档”处理
            logger.warning("检查 Memory Archive 检查点失败", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # 压缩
    # ------------------------------------------------------------------

    async def compact(
        self,
        *,
        reason: str = "manual",
    ) -> PersistentCompactionResult:
        """执行一次持久化压缩。

        Args:
            reason: 触发来源，``"manual"`` 或 ``"idle"``。

        Returns:
            ``PersistentCompactionResult``。并发调用同一 Session 时立即返回
            ``status="busy"``；没有完整未归档轮次时返回
            ``status="nothing_to_compact"`` 且不调用 LLM。
        """
        lock = _lock_for(self._lock_key)
        if lock.locked():
            return PersistentCompactionResult(
                ok=False,
                status="busy",
                reason=reason,
                error="当前 Session 正在压缩",
            )
        async with lock:
            return await self._compact_locked(reason=reason)

    async def _compact_locked(
        self,
        *,
        reason: str,
    ) -> PersistentCompactionResult:
        """持锁执行完整压缩流程。"""
        try:
            session = self.session_manager.load()
        except Exception as exc:  # noqa: BLE001 - 加载失败不能影响旧文件
            logger.warning("压缩前读取 Session 失败：%s", exc, exc_info=True)
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                error=f"读取 Session 失败：{type(exc).__name__}: {exc}",
            )

        messages = session.messages
        boundary = session.summary_boundary
        turn_groups = _complete_turn_groups(messages[boundary:])
        if not turn_groups:
            if self._has_pending_archive(session):
                try:
                    archive_cursor = self.memory_store.append_history(
                        session.summary,
                        session_key=session.key,
                        checkpoint_id=session.summary_checkpoint_id,
                    )
                except Exception as exc:  # noqa: BLE001 - 补归档失败不破坏 Session
                    logger.warning("补偿归档 Session Summary 失败：%s", exc)
                    return PersistentCompactionResult(
                        ok=False,
                        status="failed",
                        reason=reason,
                        boundary=boundary,
                        summary=session.summary,
                        checkpoint_id=session.summary_checkpoint_id,
                        error=(
                            "Memory Archive 补偿归档失败："
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                return PersistentCompactionResult(
                    ok=True,
                    status="nothing_to_compact",
                    reason=reason,
                    boundary=boundary,
                    summary=session.summary,
                    checkpoint_id=session.summary_checkpoint_id,
                    archive_cursor=archive_cursor,
                )
            return PersistentCompactionResult(
                ok=True,
                status="nothing_to_compact",
                reason=reason,
                boundary=boundary,
            )

        # ContextCompactor 的 _build_transcript 在超出预算时会丢掉最旧条目
        # （只保留最近内容）。如果直接传入全部轮次，boundary 会把没有被
        # 摘要覆盖的消息也标记为已归档。这里只选择“能完整放进 transcript”
        # 的连续轮次前缀，剩余轮次留给下一次压缩。
        prefix_messages = self._select_summary_prefix(session, turn_groups)
        if not prefix_messages:
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                boundary=boundary,
                error=(
                    "摘要输入预算不足以完整覆盖任何完整轮次，"
                    "已跳过本次压缩"
                ),
            )

        summary_input = self._build_summary_input(session, prefix_messages)
        try:
            raw_outcome = await self.compactor.compact(
                summary_input,
                self.context_config.available_input_tokens,
                protected_start=len(summary_input),
            )
        except Exception as exc:  # noqa: BLE001 - 摘要失败必须安全回退
            logger.warning("持久化摘要调用失败：%s", exc, exc_info=True)
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                error=f"摘要调用失败：{type(exc).__name__}: {exc}",
            )

        outcome = self._resolve_outcome(raw_outcome)
        if outcome is None:
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                error="摘要器未返回有效结果",
            )
        if not outcome.compacted:
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                error=outcome.error or "摘要未生成",
            )

        summary_text = outcome.summary_text
        if not isinstance(summary_text, str) or not summary_text.strip():
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                error="摘要结果缺少合法的纯摘要正文",
            )
        summary_text = summary_text.strip()

        # 只按真正送进完整 transcript 的消息推进 boundary；不能使用
        # outcome.summarized_messages（它是 ContextCompactor 对传入历史的
        # 全部计数，若 transcript 被裁剪会高估覆盖范围）。
        summarized_messages = len(prefix_messages)
        new_boundary = boundary + summarized_messages
        if new_boundary > len(messages):
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                error="摘要边界越界，已放弃保存",
            )

        checkpoint_id = _generate_checkpoint_id(
            session.key,
            new_boundary,
            summary_text,
        )
        # 关键顺序：先构造新 Session 并单次原子保存，旧对象/旧文件不受影响。
        # messages 保持完整原始历史，压缩只移动 summary_boundary。
        new_session = Session(
            key=session.key,
            messages=deepcopy(messages),
            summary=summary_text,
            summary_boundary=new_boundary,
            summary_checkpoint_id=checkpoint_id,
        )
        try:
            self.session_manager.save(new_session)
        except Exception as exc:  # noqa: BLE001 - 保存失败时 Session 保持原样
            logger.warning("保存 Session Summary 失败：%s", exc, exc_info=True)
            return PersistentCompactionResult(
                ok=False,
                status="failed",
                reason=reason,
                error=f"Session 保存失败：{type(exc).__name__}: {exc}",
            )

        archive_cursor: int | None = None
        archive_error: str | None = None
        if self.memory_store is not None:
            try:
                archive_cursor = self.memory_store.append_history(
                    summary_text,
                    session_key=session.key,
                    checkpoint_id=checkpoint_id,
                )
            except Exception as exc:  # noqa: BLE001 - 归档失败不影响 Session 成功
                logger.warning(
                    "Memory Archive 归档失败（Session 已保存）：%s",
                    exc,
                    exc_info=True,
                )
                archive_error = (
                    f"Memory Archive 归档失败：{type(exc).__name__}: {exc}"
                )

        return PersistentCompactionResult(
            ok=True,
            status="compacted",
            reason=reason,
            summarized_messages=summarized_messages,
            summary=summary_text,
            boundary=new_boundary,
            checkpoint_id=checkpoint_id,
            archive_cursor=archive_cursor,
            error=archive_error,
        )

    def _resolve_outcome(
        self,
        raw_outcome: Any,
    ) -> CompactionOutcome | None:
        """兼容 ``ContextCompactor.compact`` 的两种返回形式。

        默认返回消息列表，具体结果在 ``self.compactor.last_outcome``；也允许
        自定义 Compactor 直接返回 ``CompactionOutcome``。
        """
        if isinstance(raw_outcome, CompactionOutcome):
            return raw_outcome
        outcome = getattr(self.compactor, "last_outcome", None)
        if isinstance(outcome, CompactionOutcome):
            return outcome
        return None

    def _select_summary_prefix(
        self,
        session: Session,
        turn_groups: list[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """选择能完整进入摘要 transcript 的最长连续轮次前缀。

        ``ContextCompactor._build_transcript`` 会按固定上限截断每条消息
        （user ≤ 500、tool ≤ 300 tokens，assistant 按 max_tokens 的 60% 且
        限制在 800..4000，tool call 参数按 300 字符），并在 transcript 超
        预算时丢弃最旧条目。为避免“LLM 没看到却被标记已归档”，这里用完全
        相同的渲染规则预先估算：只有整轮渲染后仍不超过 transcript 上限的
        连续前缀才会被送入摘要。若第一轮都放不下则返回空列表。
        """
        budget = self.context_config.available_input_tokens
        summary_system_prompt = getattr(
            self.compactor,
            "summary_system_prompt",
            SUMMARY_SYSTEM_PROMPT,
        )
        if not isinstance(summary_system_prompt, str) or not summary_system_prompt:
            summary_system_prompt = SUMMARY_SYSTEM_PROMPT
        max_tokens = _summary_transcript_max_tokens(
            summary_system_prompt,
            budget,
        )
        assistant_cap = max(800, min(4000, int(max_tokens * 0.6)))

        entries: list[str] = []
        previous_summary = session.summary
        if isinstance(previous_summary, str) and previous_summary.strip():
            # 与 _build_transcript 对 prior_summaries 的渲染完全一致。
            entries.append(
                "[Earlier compacted summary — preserve these facts]\n"
                + previous_summary
            )

        selected: list[dict[str, Any]] = []
        for turn in turn_groups:
            candidate_messages = [*selected, *turn]
            call_index = _tool_call_index(candidate_messages)
            candidate_entries = [
                *entries,
                *[
                    _render_transcript_entry(
                        message,
                        assistant_cap=assistant_cap,
                        call_index=call_index,
                    )
                    for message in turn
                ],
            ]
            if (
                estimate_tokens("\n\n".join(candidate_entries))
                > max_tokens
            ):
                break
            selected = candidate_messages
            entries = candidate_entries
        return selected

    @staticmethod
    def _build_summary_input(
        session: Session,
        turns: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """构造摘要输入：旧 summary 消息（若有）+ 本轮要归档消息。

        全部输入都位于 ``protected_start`` 之前，让 ``ContextCompactor``
        把这段历史视为可压缩区；``protected_start=len(input)`` 表示没有
        受保护的当前 Delta。
        """
        summary_input: list[dict[str, Any]] = []
        previous_summary = session.summary
        if isinstance(previous_summary, str) and previous_summary.strip():
            summary_input.append(
                {
                    "role": "user",
                    "content": previous_summary,
                    "compaction_summary": True,
                }
            )
        summary_input.extend(deepcopy(turns))
        return summary_input


__all__ = [
    "PersistentCompactionResult",
    "SessionCompactor",
]
