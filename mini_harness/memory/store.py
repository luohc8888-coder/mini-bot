"""长期记忆与 Memory Archive 的纯文件持久化。

``MemoryStore`` 只负责 ``memory/`` 目录下的文件 I/O，不感知 LLM、Agent 或
Session：它被 ``SessionCompactor`` 用于归档摘要历史，被 ``DreamManager``
用于读取未处理归档并写回长期记忆。

目录结构::

    memory/
    ├── MEMORY.md       长期记忆正文（Dream 维护）
    ├── history.jsonl   归档历史，每行一个 JSON 记录，cursor 单调递增
    └── .dream_cursor   Dream 已处理到的最新 history cursor

设计约束：
- 所有文件写入都使用“同目录临时文件 + fsync + ``os.replace``”的原子替换，
  失败时原文件保持不变；
- ``history.jsonl`` 追加在模块级 ``threading.Lock`` 保护下完成“读取最大
  cursor + 追加一行”，cursor 从 1 开始严格单调递增；
- 同一非空 ``checkpoint_id`` 重复追加必须幂等；
- 单行损坏的 history 记录在读取时跳过，不影响其他记录。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_MEMORY_FILE_NAME = "MEMORY.md"
_HISTORY_FILE_NAME = "history.jsonl"
_DREAM_CURSOR_FILE_NAME = ".dream_cursor"

# 进程内串行化所有 MemoryStore 实例的 history 追加。单实例锁无法覆盖
# “同一进程创建多个 MemoryStore 指向同一目录”的情况，因此使用模块级锁。
_HISTORY_APPEND_LOCK = threading.Lock()


class MemoryStoreError(RuntimeError):
    """MemoryStore 文件读写失败或数据非法。"""


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """``history.jsonl`` 中的一条归档记录。

    Args:
        cursor: 单调递增的归档游标，从 1 开始。
        session_key: 产生该摘要的 Session Key。
        timestamp: 归档时间的 ISO-8601 字符串。
        content: 归档的摘要正文。
        checkpoint_id: 摘要检查点稳定 ID；空字符串表示不做去重。
    """

    cursor: int
    session_key: str
    timestamp: str
    content: str
    checkpoint_id: str = ""


class MemoryStore:
    """``memory/`` 目录的纯文件 I/O 封装。

    Args:
        workspace: Workspace 根目录；默认把 ``memory/`` 放在其下。
        root: 可选的记忆根目录覆盖，便于测试或多 Workspace 共享记忆。
    """

    def __init__(self, workspace: Path, *, root: Path | None = None) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        if root is None:
            self.root = self.workspace / "memory"
        else:
            self.root = Path(root).expanduser().resolve()

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    @property
    def memory_dir(self) -> Path:
        """记忆根目录（``memory/``）。"""
        return self.root

    @property
    def memory_file(self) -> Path:
        """长期记忆文件 ``MEMORY.md``。"""
        return self.root / _MEMORY_FILE_NAME

    @property
    def history_file(self) -> Path:
        """归档历史文件 ``history.jsonl``。"""
        return self.root / _HISTORY_FILE_NAME

    @property
    def dream_cursor_file(self) -> Path:
        """Dream 处理进度文件 ``.dream_cursor``。"""
        return self.root / _DREAM_CURSOR_FILE_NAME

    # ------------------------------------------------------------------
    # MEMORY.md
    # ------------------------------------------------------------------

    def read_memory(self) -> str:
        """读取 ``MEMORY.md``；文件不存在时返回空字符串。

        Raises:
            MemoryStoreError: 文件存在但无法读取或不是合法 UTF-8。
        """
        try:
            return self.memory_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except (OSError, UnicodeDecodeError) as exc:
            raise MemoryStoreError(
                f"无法读取长期记忆文件：{self.memory_file}"
            ) from exc

    def write_memory(self, content: str) -> None:
        """原子写入 ``MEMORY.md``。

        目录不存在时自动创建；写入失败时旧文件保持不变。

        Raises:
            MemoryStoreError: ``content`` 不是字符串或写入失败。
        """
        if not isinstance(content, str):
            raise MemoryStoreError("MEMORY.md 内容必须是字符串")
        self._ensure_memory_dir()
        self._atomic_write_text(self.memory_file, content)

    # ------------------------------------------------------------------
    # history.jsonl
    # ------------------------------------------------------------------

    def append_history(
        self,
        content: str,
        *,
        session_key: str,
        checkpoint_id: str = "",
        timestamp: str | None = None,
    ) -> int:
        """向 ``history.jsonl`` 追加一条归档记录并返回其 cursor。

        Args:
            content: 摘要正文。
            session_key: 产生摘要的 Session Key。
            checkpoint_id: 非空时用于幂等去重；相同 ID 不会重复追加。
            timestamp: ISO-8601 时间字符串；缺省使用当前本地时间。

        Returns:
            新记录的 cursor；若 ``checkpoint_id`` 已存在则返回已有记录的 cursor。

        Raises:
            MemoryStoreError: 参数类型错误或文件写入失败。
        """
        if not isinstance(content, str):
            raise MemoryStoreError("history content 必须是字符串")
        if not isinstance(session_key, str):
            raise MemoryStoreError("history session_key 必须是字符串")
        if not isinstance(checkpoint_id, str):
            raise MemoryStoreError("history checkpoint_id 必须是字符串")
        if timestamp is not None and not isinstance(timestamp, str):
            raise MemoryStoreError("history timestamp 必须是字符串")

        with _HISTORY_APPEND_LOCK:
            records = self._load_history_records()
            if checkpoint_id:
                for record in records:
                    if record.checkpoint_id == checkpoint_id:
                        return record.cursor

            cursor = max((record.cursor for record in records), default=0) + 1
            if timestamp is None:
                timestamp = datetime.now().astimezone().isoformat()
            record = HistoryRecord(
                cursor=cursor,
                session_key=session_key,
                timestamp=timestamp,
                content=content,
                checkpoint_id=checkpoint_id,
            )
            # 在原始文本后追加，保留文件中无法解析的损坏行原样不动。
            existing = ""
            if self.history_file.exists():
                try:
                    existing = self.history_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise MemoryStoreError(
                        f"无法读取归档历史文件：{self.history_file}"
                    ) from exc
            if existing and not existing.endswith("\n"):
                existing += "\n"
            line = json.dumps(
                self._record_to_dict(record),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._ensure_memory_dir()
            self._atomic_write_text(
                self.history_file, f"{existing}{line}\n"
            )
            return cursor

    def read_history(
        self,
        *,
        since_cursor: int = 0,
        limit: int | None = None,
    ) -> list[HistoryRecord]:
        """读取归档历史，按 cursor 升序返回。

        Args:
            since_cursor: 只返回 ``cursor > since_cursor`` 的记录（开区间）。
            limit: 最多返回的记录数；``None`` 表示不限制。

        Returns:
            合法记录列表；单行损坏的记录会被跳过。

        Raises:
            MemoryStoreError: 文件无法读取。
        """
        records = self._load_history_records()
        if since_cursor > 0:
            records = [
                record for record in records if record.cursor > since_cursor
            ]
        if limit is not None:
            if limit <= 0:
                return []
            records = records[:limit]
        return records

    def latest_cursor(self) -> int:
        """返回当前合法记录中的最大 cursor；没有记录时返回 0。"""
        records = self._load_history_records()
        return max((record.cursor for record in records), default=0)

    def has_checkpoint(self, checkpoint_id: str) -> bool:
        """判断非空 ``checkpoint_id`` 是否已归档；空字符串返回 False。"""
        if not checkpoint_id:
            return False
        return any(
            record.checkpoint_id == checkpoint_id
            for record in self._load_history_records()
        )

    # ------------------------------------------------------------------
    # .dream_cursor
    # ------------------------------------------------------------------

    def get_dream_cursor(self) -> int:
        """读取 Dream cursor；文件缺失或内容非法时返回 0。"""
        try:
            raw = self.dream_cursor_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return 0
        except (OSError, UnicodeDecodeError):
            return 0
        if not raw:
            return 0
        try:
            value = int(raw)
        except ValueError:
            return 0
        if value < 0:
            return 0
        return value

    def set_dream_cursor(self, cursor: int) -> None:
        """原子写入 Dream cursor。

        Raises:
            MemoryStoreError: ``cursor`` 不是非负整数或写入失败。
        """
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            raise MemoryStoreError("dream cursor 必须是整数")
        if cursor < 0:
            raise MemoryStoreError("dream cursor 不能为负数")
        self._ensure_memory_dir()
        self._atomic_write_text(self.dream_cursor_file, f"{cursor}\n")

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _ensure_memory_dir(self) -> None:
        """创建记忆目录；失败时抛出 MemoryStoreError。"""
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemoryStoreError(
                f"无法创建记忆目录：{self.memory_dir}"
            ) from exc

    def _load_history_records(self) -> list[HistoryRecord]:
        """读取并解析全部合法 history 记录；损坏行被跳过。"""
        try:
            raw = self.history_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            raise MemoryStoreError(
                f"无法读取归档历史文件：{self.history_file}"
            ) from exc
        records: list[HistoryRecord] = []
        for line in raw.splitlines():
            record = self._parse_history_line(line)
            if record is not None:
                records.append(record)
        # 正常追加顺序即 cursor 升序；这里再稳定排序一次，防止人工编辑或
        # 外部写入导致乱序影响 Dream 的增量处理。
        records.sort(key=lambda record: record.cursor)
        return records

    @staticmethod
    def _parse_history_line(line: str) -> HistoryRecord | None:
        """解析单行 history；空行或损坏行返回 None。"""
        text = line.strip()
        if not text:
            return None
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        cursor = payload.get("cursor")
        session_key = payload.get("session_key")
        timestamp = payload.get("timestamp")
        content = payload.get("content")
        checkpoint_id = payload.get("checkpoint_id", "")
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            return None
        if cursor < 1:
            return None
        if not isinstance(session_key, str):
            return None
        if not isinstance(timestamp, str):
            return None
        if not isinstance(content, str):
            return None
        if not isinstance(checkpoint_id, str):
            return None
        return HistoryRecord(
            cursor=cursor,
            session_key=session_key,
            timestamp=timestamp,
            content=content,
            checkpoint_id=checkpoint_id,
        )

    @staticmethod
    def _record_to_dict(record: HistoryRecord) -> dict[str, Any]:
        """把 HistoryRecord 转成 JSON 可序列化字典。"""
        return {
            "cursor": record.cursor,
            "session_key": record.session_key,
            "timestamp": record.timestamp,
            "content": record.content,
            "checkpoint_id": record.checkpoint_id,
        }

    def _atomic_write_text(self, target: Path, content: str) -> None:
        """用同目录临时文件 + fsync + ``os.replace`` 原子写入文本。

        写入失败时清理临时文件并抛出 ``MemoryStoreError``，目标文件保持
        原有内容；已有文件会保留原权限位，新文件默认 0o600。
        """
        existing_mode: int | None = None
        try:
            existing_mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise MemoryStoreError(
                f"无法读取文件状态：{target}"
            ) from exc

        temp_path: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                dir=str(target.parent),
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(
                temp_path,
                existing_mode if existing_mode is not None else 0o600,
            )
            os.replace(temp_path, target)
            temp_path = None
            self._fsync_directory(target.parent)
        except OSError as exc:
            raise MemoryStoreError(f"无法写入文件：{target}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """尽量 fsync 目录；部分平台不支持目录 fsync，忽略该错误。"""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


__all__ = [
    "HistoryRecord",
    "MemoryStore",
    "MemoryStoreError",
]
