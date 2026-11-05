"""MemoryStore 文件持久化、cursor 单调性与 checkpoint 幂等测试。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

import mini_harness.memory.store as store_module
from mini_harness.memory import HistoryRecord, MemoryStore, MemoryStoreError


def make_store(tmp_path: Path) -> MemoryStore:
    """创建指向临时 Workspace 的 MemoryStore。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return MemoryStore(workspace)


def write_history_lines(store: MemoryStore, lines: list[str]) -> None:
    """直接写入原始 history 文本，用于构造损坏行场景。"""
    store.memory_dir.mkdir(parents=True, exist_ok=True)
    store.history_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def test_default_paths_use_workspace_memory_dir(tmp_path: Path) -> None:
    """默认根目录是 <workspace>/memory，三个文件路径稳定。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MemoryStore(workspace)

    assert store.memory_dir == workspace / "memory"
    assert store.memory_file == workspace / "memory" / "MEMORY.md"
    assert store.history_file == workspace / "memory" / "history.jsonl"
    assert store.dream_cursor_file == workspace / "memory" / ".dream_cursor"

    override = tmp_path / "custom-memory"
    custom = MemoryStore(workspace, root=override)
    assert custom.memory_dir == override


def test_read_memory_missing_returns_empty_without_creating_dir(
    tmp_path: Path,
) -> None:
    """MEMORY.md 不存在时返回空字符串，且读操作不创建目录。"""
    store = make_store(tmp_path)
    assert store.read_memory() == ""
    assert not store.memory_dir.exists()


def test_write_and_read_memory_roundtrip(tmp_path: Path) -> None:
    """write_memory 自动建目录，读写内容一致。"""
    store = make_store(tmp_path)
    content = "# 长期记忆\n\n## 用户偏好\n- 使用中文\n"

    store.write_memory(content)

    assert store.memory_file.read_text(encoding="utf-8") == content
    assert store.read_memory() == content


def test_write_memory_failure_keeps_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子写入失败时旧 MEMORY.md 不被破坏，临时文件被清理。"""
    store = make_store(tmp_path)
    store.write_memory("旧记忆\n")
    old_text = store.memory_file.read_text(encoding="utf-8")

    real_replace = store_module.os.replace

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(store_module.os, "replace", failing_replace)
    with pytest.raises(MemoryStoreError):
        store.write_memory("新记忆\n")

    monkeypatch.setattr(store_module.os, "replace", real_replace)
    assert store.memory_file.read_text(encoding="utf-8") == old_text
    assert list(store.memory_dir.glob("*.tmp")) == []


def test_append_history_assigns_monotonic_cursors(tmp_path: Path) -> None:
    """cursor 从 1 开始严格递增，记录字段完整且可按游标读取。"""
    store = make_store(tmp_path)

    assert store.append_history("第一条", session_key="cli:main") == 1
    assert store.append_history("第二条", session_key="cli:main") == 2
    assert store.append_history("第三条", session_key="other") == 3

    records = store.read_history()
    assert [record.cursor for record in records] == [1, 2, 3]
    assert isinstance(records[0], HistoryRecord)
    assert records[1].content == "第二条"
    assert records[1].session_key == "cli:main"
    assert records[2].session_key == "other"
    # 默认 timestamp 是合法 ISO-8601 字符串。
    datetime.fromisoformat(records[0].timestamp)
    assert store.latest_cursor() == 3


def test_append_history_checkpoint_idempotent(tmp_path: Path) -> None:
    """同一非空 checkpoint_id 重复 append 不再追加，返回已有 cursor。"""
    store = make_store(tmp_path)

    assert store.append_history(
        "摘要", session_key="cli:main", checkpoint_id="cp-001"
    ) == 1
    assert store.append_history(
        "摘要（内容变化也不重复）", session_key="cli:main", checkpoint_id="cp-001"
    ) == 1

    assert len(store.read_history()) == 1
    assert store.has_checkpoint("cp-001") is True
    assert store.has_checkpoint("cp-other") is False
    # 空 checkpoint_id 不做去重，空字符串永远不算已归档。
    assert store.has_checkpoint("") is False
    assert store.append_history("a", session_key="k") == 2
    assert store.append_history("b", session_key="k") == 3


def test_append_history_skips_corrupt_lines_and_continues_cursor(
    tmp_path: Path,
) -> None:
    """损坏行被跳过，后续追加 cursor 基于最大合法 cursor + 1。"""
    store = make_store(tmp_path)
    write_history_lines(
        store,
        [
            json.dumps(
                {
                    "cursor": 1,
                    "session_key": "k",
                    "timestamp": "2024-01-01T00:00:00+08:00",
                    "content": "valid-1",
                }
            ),
            "{ this is not valid json",
            json.dumps(
                {
                    "cursor": 3,
                    "session_key": "k",
                    "timestamp": "2024-01-02T00:00:00+08:00",
                    "content": "valid-3",
                }
            ),
        ],
    )

    assert store.append_history("new", session_key="k") == 4
    records = store.read_history()
    assert [record.cursor for record in records] == [1, 3, 4]
    assert store.latest_cursor() == 4


def test_read_history_skips_malformed_records(tmp_path: Path) -> None:
    """非法 JSON、结构错误/字段类型错误的行全部跳过。"""
    store = make_store(tmp_path)
    write_history_lines(
        store,
        [
            json.dumps(
                {
                    "cursor": 1,
                    "session_key": "k",
                    "timestamp": "2024-01-01T00:00:00+08:00",
                    "content": "ok",
                }
            ),
            "",
            "[1, 2, 3]",
            '"just a string"',
            json.dumps({"session_key": "k", "content": "missing cursor"}),
            json.dumps(
                {
                    "cursor": "2",
                    "session_key": "k",
                    "timestamp": "2024-01-01T00:00:00+08:00",
                    "content": "cursor 不是 int",
                }
            ),
            json.dumps(
                {
                    "cursor": 3,
                    "session_key": "k",
                    "timestamp": "2024-01-01T00:00:00+08:00",
                    "content": "ok-3",
                }
            ),
        ],
    )

    records = store.read_history()
    assert [record.cursor for record in records] == [1, 3]


def test_read_history_since_cursor_and_limit(tmp_path: Path) -> None:
    """since_cursor 为开区间，limit 截断结果。"""
    store = make_store(tmp_path)
    for index in range(5):
        store.append_history(f"content-{index}", session_key="k")

    assert [record.cursor for record in store.read_history(since_cursor=2)] == [3, 4, 5]
    assert [record.cursor for record in store.read_history(limit=2)] == [1, 2]
    assert [
        record.cursor
        for record in store.read_history(since_cursor=2, limit=2)
    ] == [3, 4]
    assert store.read_history(since_cursor=5) == []
    assert store.read_history(limit=0) == []
    assert store.read_history(limit=-1) == []


def test_append_history_handles_missing_trailing_newline(tmp_path: Path) -> None:
    """已有文件末尾无换行时，追加记录不会与上一行粘连。"""
    store = make_store(tmp_path)
    store.memory_dir.mkdir(parents=True, exist_ok=True)
    store.history_file.write_text(
        json.dumps(
            {
                "cursor": 1,
                "session_key": "k",
                "timestamp": "2024-01-01T00:00:00+08:00",
                "content": "old",
            }
        ),
        encoding="utf-8",
    )

    assert store.append_history("new", session_key="k") == 2
    records = store.read_history()
    assert [record.cursor for record in records] == [1, 2]
    assert [record.content for record in records] == ["old", "new"]


def test_append_history_failure_keeps_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """追加写入失败时 history.jsonl 原内容保持不变。"""
    store = make_store(tmp_path)
    store.append_history("旧记录", session_key="k", checkpoint_id="old")
    old_text = store.history_file.read_text(encoding="utf-8")

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(store_module.os, "replace", failing_replace)
    with pytest.raises(MemoryStoreError):
        store.append_history("新记录", session_key="k")

    monkeypatch.undo()
    assert store.history_file.read_text(encoding="utf-8") == old_text
    assert [record.content for record in store.read_history()] == ["旧记录"]


def test_dream_cursor_roundtrip_and_persistence(tmp_path: Path) -> None:
    """Dream cursor 写入后可跨 MemoryStore 实例读取。"""
    store = make_store(tmp_path)
    assert store.get_dream_cursor() == 0

    store.set_dream_cursor(7)

    assert store.dream_cursor_file.read_text(encoding="utf-8") == "7\n"
    assert store.get_dream_cursor() == 7
    assert MemoryStore(store.workspace, root=store.memory_dir).get_dream_cursor() == 7


@pytest.mark.parametrize("content", ["abc", "", "-1", "2.5", "  \n", "NaN"])
def test_dream_cursor_invalid_content_returns_zero(
    tmp_path: Path, content: str
) -> None:
    """缺失/非法 .dream_cursor 内容按 0 处理。"""
    store = make_store(tmp_path)
    store.memory_dir.mkdir(parents=True, exist_ok=True)
    store.dream_cursor_file.write_text(content, encoding="utf-8")
    assert store.get_dream_cursor() == 0


def test_set_dream_cursor_failure_keeps_previous_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子写失败时旧 .dream_cursor 保持不变。"""
    store = make_store(tmp_path)
    store.set_dream_cursor(5)

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(store_module.os, "replace", failing_replace)
    with pytest.raises(MemoryStoreError):
        store.set_dream_cursor(9)

    monkeypatch.undo()
    assert store.get_dream_cursor() == 5
    assert list(store.memory_dir.glob("*.tmp")) == []


def test_set_dream_cursor_rejects_invalid_values(tmp_path: Path) -> None:
    """dream cursor 只接受非负整数。"""
    store = make_store(tmp_path)
    for value in (-1, 1.5, "3", True):
        with pytest.raises(MemoryStoreError):
            store.set_dream_cursor(value)  # type: ignore[arg-type]


def test_concurrent_appends_produce_unique_monotonic_cursors(
    tmp_path: Path,
) -> None:
    """多线程并发 append 不产生重复/跳号 cursor。"""
    store = make_store(tmp_path)

    def append(index: int) -> int:
        return store.append_history(f"content-{index}", session_key="k")

    with ThreadPoolExecutor(max_workers=8) as executor:
        cursors = list(executor.map(append, range(20)))

    assert sorted(cursors) == list(range(1, 21))
    records = store.read_history()
    assert [record.cursor for record in records] == list(range(1, 21))
