"""长期记忆文档模型、响应解析与确定性增删改。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# 固定 section 顺序；未知 section 归类为“其他”。
DREAM_SECTIONS: tuple[str, ...] = (
    "用户偏好",
    "项目决策",
    "架构约定",
    "长期目标",
    "经验知识",
    "其他",
)

# 单条记忆超过此长度时拒绝，避免写入整段对话或工具输出。
MAX_ENTRY_CHARS = 2000

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
    normalized = text.replace("（", "(").replace("）", ")")
    normalized = normalized.strip(" \t\r\n。.!！;；?？,，")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if normalized.casefold() in {"nothing", "none", "无"}:
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
