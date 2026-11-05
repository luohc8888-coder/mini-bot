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
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from mini_harness.providers.base import LLMProvider

from .store import HistoryRecord, MemoryStore

logger = logging.getLogger(__name__)


# Dream 允许写入的固定 section；未知 section 一律归类为“其他”。这个顺序
# 同时决定写回 MEMORY.md 时各 section 的排列顺序，保证输出稳定可预期。
DREAM_SECTIONS: tuple[str, ...] = (
    "用户偏好",
    "项目决策",
    "架构约定",
    "长期目标",
    "经验知识",
    "其他",
)

# 单条记忆文本的硬上限。超过 2000 字符通常意味着模型把整段对话或工具
# 输出抄了进来，必须拒绝而不是写入 MEMORY.md。
MAX_ENTRY_CHARS = 2000

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


# ---------------------------------------------------------------------------
# MEMORY.md 文档模型与确定性操作
# ---------------------------------------------------------------------------


_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^[-*+](?:\s+|$)")
_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _normalize_text(value: str) -> str:
    """规范化单条记忆：去首尾空白、去 bullet 前缀、压缩连续空白。"""
    text = value.strip()
    text = _BULLET_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _bullet_text(line: str) -> str | None:
    """若 ``line`` 是 bullet 行则返回去掉前缀的正文，否则返回 None。"""
    match = _BULLET_RE.match(line)
    if match is None:
        return None
    return line[match.end() :].strip()


def _validate_entry_text(value: object) -> str | None:
    """校验 LLM 给出的单条文本，合法时返回规范化文本。

    以下情况视为非法条目（调用方计入 ``skipped``）：
    - 不是字符串或规范化后为空；
    - 超过 ``MAX_ENTRY_CHARS`` 字符；
    - 含换行、制表符等控制字符（防止把多行内容或日志塞进单条记忆）。
    """
    if not isinstance(value, str):
        return None
    if _CONTROL_CHARS_RE.search(value):
        return None
    normalized = _normalize_text(value)
    if not normalized:
        return None
    if len(normalized) > MAX_ENTRY_CHARS:
        return None
    return normalized


@dataclass(slots=True)
class _MemorySection:
    """MEMORY.md 中的一个二级标题 section。"""

    title: str
    entries: list[str] = field(default_factory=list)
    extra_lines: list[str] = field(default_factory=list)


class _MemoryDocument:
    """MEMORY.md 的可编辑模型。

    只解析本模块约定格式（``## section`` + ``- 条目``）；标题前的普通文本
    作为 preamble 原样保留，标题下的非 bullet 行作为 extra_lines 保留，
    避免 Dream 误删用户手写的说明文字。
    """

    def __init__(
        self,
        *,
        preamble: list[str] | None = None,
        sections: list[_MemorySection] | None = None,
    ) -> None:
        self.preamble = list(preamble or [])
        self.sections = list(sections or [])

    # -- 解析与渲染 -----------------------------------------------------

    @classmethod
    def parse(cls, content: str) -> "_MemoryDocument":
        """把 MEMORY.md 文本解析成文档模型。"""
        preamble: list[str] = []
        pending_preamble: list[str] = []
        sections: list[_MemorySection] = []
        current: _MemorySection | None = None

        for raw_line in content.splitlines():
            line = raw_line.rstrip()
            heading = _SECTION_HEADING_RE.match(line)
            if heading is not None:
                current = _MemorySection(title=heading.group(1).strip())
                sections.append(current)
                continue
            if current is None:
                pending_preamble.append(line)
                continue
            _append_section_line(current, line)

        # 标题前的 bullet 归入“其他”section，避免手写 MEMORY.md 丢失条目；
        # 其余行保留为 preamble（例如 "# 长期记忆" 标题或说明文字）。
        preamble_bullets: list[str] = []
        for line in pending_preamble:
            bullet = _bullet_text(line.strip())
            if bullet is None:
                preamble.append(line)
            elif bullet:
                preamble_bullets.append(bullet)
        if preamble_bullets:
            other = next(
                (section for section in sections if section.title == "其他"),
                None,
            )
            if other is None:
                other = _MemorySection(title="其他")
                sections.append(other)
            other.entries = preamble_bullets + other.entries

        return cls(preamble=preamble, sections=sections)

    def render(self) -> str:
        """把文档模型渲染成稳定的 MEMORY.md 文本。"""
        lines: list[str] = []

        preamble = list(self.preamble)
        while preamble and not preamble[0].strip():
            preamble.pop(0)
        while preamble and not preamble[-1].strip():
            preamble.pop()
        if preamble:
            lines.extend(preamble)
        elif self._has_entries():
            # 新建/空文件首次写入时补一个总标题，保持 MEMORY.md 可读。
            lines.append("# 长期记忆")

        ordered_sections = self._ordered_sections()
        for section in ordered_sections:
            entries = [
                normalized
                for normalized in (
                    _normalize_text(entry) for entry in section.entries
                )
                if normalized
            ]
            if not entries and not section.extra_lines:
                continue
            if lines:
                lines.append("")
            lines.append(f"## {section.title}")
            for entry in entries:
                lines.append(f"- {entry}")
            lines.extend(section.extra_lines)

        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    # -- 查询与修改 -----------------------------------------------------

    def _has_entries(self) -> bool:
        return any(
            _normalize_text(entry)
            for section in self.sections
            for entry in section.entries
        )

    def _ordered_sections(self) -> list[_MemorySection]:
        """按固定 section 顺序输出；未知标题保持原有相对顺序追加在后。"""
        ordered: list[_MemorySection] = []
        used: set[int] = set()
        for title in DREAM_SECTIONS:
            for section in self.sections:
                if id(section) in used:
                    continue
                if section.title == title:
                    ordered.append(section)
                    used.add(id(section))
                    break
        for section in self.sections:
            if id(section) not in used:
                ordered.append(section)
                used.add(id(section))
        return ordered

    def _ensure_section(self, title: str) -> _MemorySection:
        for section in self.sections:
            if section.title == title:
                return section
        section = _MemorySection(title=title)
        self.sections.append(section)
        return section

    def find(self, normalized: str) -> tuple[_MemorySection, int] | None:
        """查找规范化后等于 ``normalized`` 的第一条记忆。"""
        if not normalized:
            return None
        for section in self.sections:
            for index, entry in enumerate(section.entries):
                if _normalize_text(entry) == normalized:
                    return section, index
        return None

    def add(self, section_title: str, text: str) -> None:
        self._ensure_section(section_title).entries.append(text)

    def replace(
        self, location: tuple[_MemorySection, int], new_text: str
    ) -> None:
        section, index = location
        section.entries[index] = new_text

    def remove(self, location: tuple[_MemorySection, int]) -> None:
        section, index = location
        del section.entries[index]


def _append_section_line(section: _MemorySection, line: str) -> None:
    """把 section 下的一行文本归类为 bullet 条目或原样保留行。"""
    stripped = line.strip()
    if not stripped:
        return
    bullet = _bullet_text(stripped)
    if bullet is not None:
        if bullet:
            section.entries.append(bullet)
    else:
        section.extra_lines.append(line)


# ---------------------------------------------------------------------------
# JSON 解析
# ---------------------------------------------------------------------------


def _strip_json_fence(text: str) -> str:
    """去掉可选的 Markdown ```json ... ``` 围栏。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _empty_operations() -> dict[str, list[Any]]:
    return {"add": [], "update": [], "remove": []}


def _parse_operations(content: str) -> dict[str, list[Any]]:
    """解析 LLM 返回的 JSON 操作。

    Raises:
        ValueError: JSON 非法或顶层结构不符合契约。
    """
    text = _strip_json_fence(content)
    normalized = text.strip().strip("。.!！").strip().replace("（", "(").replace("）", ")")
    if normalized.lower() in {"(nothing)", "nothing", "none", "无"}:
        return _empty_operations()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Dream 返回非法 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Dream 返回结构非法：顶层必须是 JSON 对象")

    operations = _empty_operations()
    for key in ("add", "update", "remove"):
        value = payload.get(key, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            raise ValueError(f"Dream 返回结构非法：{key} 必须是数组")
        operations[key] = value
    return operations


# ---------------------------------------------------------------------------
# DreamManager
# ---------------------------------------------------------------------------


class DreamManager:
    """长期记忆整理引擎。

    Args:
        provider: 用于提炼记忆的 LLM Provider；CLI 切换模型时可直接替换
            ``self.provider``。
        store: MemoryStore，负责 history / MEMORY.md / cursor 的文件 I/O。
        config: DreamConfig；None 时使用默认配置。

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
    ) -> None:
        self.provider = provider
        self.store = store
        self.config = config if config is not None else DreamConfig()
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
        if current_cursor > latest_cursor:
            logger.warning(
                "Dream cursor %s 超过 history 最新 cursor %s，回退到 0 重放",
                current_cursor,
                latest_cursor,
            )
            try:
                self.store.set_dream_cursor(0)
            except Exception as exc:  # noqa: BLE001
                return DreamResult(
                    processed_entries=0,
                    cursor=current_cursor,
                    error=f"修正 Dream cursor 失败：{exc}",
                )
            current_cursor = 0
        effective_max = (
            self.config.max_entries if max_entries is None else max_entries
        )
        if effective_max <= 0:
            return DreamResult(processed_entries=0, cursor=current_cursor)

        try:
            batch = self.store.read_history(
                since_cursor=current_cursor, limit=effective_max
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

        messages = [
            {"role": "system", "content": DREAM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._build_user_message(current_memory, batch),
            },
        ]
        try:
            response = await self.provider.chat(messages=messages, tools=None)
        except Exception as exc:  # noqa: BLE001 - Provider 失败不能让进程崩溃
            logger.warning("Dream Provider 调用失败：%s", exc)
            return DreamResult(
                processed_entries=len(batch),
                cursor=current_cursor,
                llm_called=True,
                error=f"Dream Provider 调用失败：{exc}",
            )

        # Provider 契约返回 LLMResponse；这里用 getattr 兼容测试替身，
        # 同时把“非字符串/空字符串”统一视为空响应失败。
        content = getattr(response, "content", None)
        if isinstance(response, str):
            content = response
        if not isinstance(content, str) or not content.strip():
            return DreamResult(
                processed_entries=len(batch),
                cursor=current_cursor,
                llm_called=True,
                error="Dream Provider 返回空响应",
            )

        try:
            operations = _parse_operations(content)
        except ValueError as exc:
            return DreamResult(
                processed_entries=len(batch),
                cursor=current_cursor,
                llm_called=True,
                error=str(exc),
            )

        document = _MemoryDocument.parse(current_memory)
        added, updated, removed, skipped = _apply_operations(document, operations)

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


def _apply_operations(
    document: _MemoryDocument, operations: dict[str, list[Any]]
) -> tuple[int, int, int, int]:
    """把 LLM 返回的操作确定性应用到文档模型。

    Returns:
        ``(added, updated, removed, skipped)``。
    """
    added = updated = removed = skipped = 0

    for item in operations.get("add", []):
        if not isinstance(item, dict):
            skipped += 1
            continue
        text = _validate_entry_text(item.get("text"))
        if text is None:
            skipped += 1
            continue
        section = item.get("section")
        section_title = (
            section if isinstance(section, str) and section in DREAM_SECTIONS
            else "其他"
        )
        if document.find(text) is not None:
            # 规范化后与已有条目重复：跳过，保证 MEMORY.md 不重复。
            skipped += 1
            continue
        document.add(section_title, text)
        added += 1

    for item in operations.get("update", []):
        if not isinstance(item, dict):
            skipped += 1
            continue
        old = _validate_entry_text(item.get("old"))
        new = _validate_entry_text(item.get("new"))
        if old is None or new is None:
            skipped += 1
            continue
        location = document.find(old)
        if location is None:
            skipped += 1
            continue
        if old == new:
            # 内容没有变化，不算一次有效更新。
            skipped += 1
            continue
        existing = document.find(new)
        if existing is not None and existing != location:
            # 新文本已存在于其他位置：合并为一条，删除旧条目。
            document.remove(location)
        else:
            document.replace(location, new)
        updated += 1

    for item in operations.get("remove", []):
        old = _validate_entry_text(item)
        if old is None:
            skipped += 1
            continue
        location = document.find(old)
        if location is None:
            skipped += 1
            continue
        document.remove(location)
        removed += 1

    return added, updated, removed, skipped


# ---------------------------------------------------------------------------
# DreamScheduler
# ---------------------------------------------------------------------------


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


__all__ = [
    "BUSY_ERROR",
    "DREAM_SECTIONS",
    "DREAM_SYSTEM_PROMPT",
    "DreamConfig",
    "DreamManager",
    "DreamResult",
    "DreamScheduler",
    "MAX_ENTRY_CHARS",
]
