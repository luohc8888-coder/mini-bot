"""Session Summary Checkpoint 字段的持久化、兼容与校验测试。"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from mini_harness.session import (
    DEFAULT_SESSION_KEY,
    Session,
    SessionCorruptError,
    SessionManager,
)


def make_workspace(tmp_path: Path, name: str = "workspace") -> Path:
    """创建测试 Workspace。"""
    workspace = tmp_path / name
    workspace.mkdir()
    return workspace


def make_manager(tmp_path: Path) -> SessionManager:
    """创建指向临时 Workspace 的 SessionManager。"""
    return SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")


def make_messages(count: int = 2) -> list[dict[str, object]]:
    """构造指定数量的简单合法消息。"""
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
        for index in range(count)
    ]


def write_session_payload(manager: SessionManager, payload: dict[str, object]) -> None:
    """直接把自定义 JSON payload 写到 Session 路径。"""
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def base_payload(manager: SessionManager, messages: list[dict[str, object]]) -> dict[str, object]:
    """构造旧 schema v1 的最小合法 payload（不含 summary 字段）。"""
    return {
        "schema_version": 1,
        "workspace": str(manager.workspace),
        "session_key": DEFAULT_SESSION_KEY,
        "messages": messages,
    }


def test_save_and_reload_restores_summary_checkpoint(tmp_path: Path) -> None:
    """summary / boundary / checkpoint_id 必须完整保存和恢复。"""
    manager = make_manager(tmp_path)
    session = Session(
        key=DEFAULT_SESSION_KEY,
        messages=make_messages(4),
        summary="用户希望实现持久化摘要。",
        summary_boundary=2,
        summary_checkpoint_id="abcdef0123456789",
    )
    manager.save(session)

    payload = json.loads(manager.path.read_text(encoding="utf-8"))
    assert payload["summary"] == "用户希望实现持久化摘要。"
    assert payload["summary_boundary"] == 2
    assert payload["summary_checkpoint_id"] == "abcdef0123456789"
    # messages 永远保存完整原始历史，不因摘要边界而删除。
    assert len(payload["messages"]) == 4

    restored = SessionManager(manager.workspace, data_root=tmp_path / "data").load()
    assert restored == session


def test_old_schema_v1_file_without_summary_fields_loads_defaults(
    tmp_path: Path,
) -> None:
    """旧 schema v1 文件缺少 summary 字段时按空摘要加载。"""
    manager = make_manager(tmp_path)
    messages = make_messages(2)
    write_session_payload(manager, base_payload(manager, messages))

    restored = manager.load()
    assert restored.summary == ""
    assert restored.summary_boundary == 0
    assert restored.summary_checkpoint_id == ""
    assert restored.messages == messages


def test_empty_summary_with_zero_boundary_is_valid(tmp_path: Path) -> None:
    """空摘要 + boundary=0 是合法默认状态。"""
    manager = make_manager(tmp_path)
    session = Session(key=DEFAULT_SESSION_KEY, messages=make_messages(2))
    manager.save(session)
    restored = manager.load()
    assert restored.summary == ""
    assert restored.summary_boundary == 0


def test_summary_with_zero_boundary_is_rejected_on_save(tmp_path: Path) -> None:
    """摘要非空时 boundary 至少为 1。"""
    manager = make_manager(tmp_path)
    session = Session(
        key=DEFAULT_SESSION_KEY,
        messages=make_messages(2),
        summary="摘要",
        summary_boundary=0,
    )
    with pytest.raises(SessionCorruptError):
        manager.save(session)
    assert not manager.path.exists()


@pytest.mark.parametrize(
    "boundary",
    [-1, 3, "1", 1.5, None, True],
)
def test_invalid_summary_boundary_is_rejected_on_load(
    tmp_path: Path, boundary: object
) -> None:
    """非法 summary_boundary（越界/类型错误）必须拒绝加载。"""
    manager = make_manager(tmp_path)
    messages = make_messages(2)
    payload = base_payload(manager, messages)
    payload["summary"] = "摘要"
    payload["summary_boundary"] = boundary
    write_session_payload(manager, payload)

    with pytest.raises(SessionCorruptError):
        manager.load()


def test_invalid_summary_type_is_rejected_on_load(tmp_path: Path) -> None:
    """summary 必须是字符串。"""
    manager = make_manager(tmp_path)
    payload = base_payload(manager, make_messages(2))
    payload["summary"] = 123
    payload["summary_boundary"] = 1
    write_session_payload(manager, payload)

    with pytest.raises(SessionCorruptError):
        manager.load()


def test_invalid_checkpoint_id_type_is_rejected_on_load(tmp_path: Path) -> None:
    """summary_checkpoint_id 必须是字符串。"""
    manager = make_manager(tmp_path)
    payload = base_payload(manager, make_messages(2))
    payload["summary"] = ""
    payload["summary_boundary"] = 0
    payload["summary_checkpoint_id"] = 42
    write_session_payload(manager, payload)

    with pytest.raises(SessionCorruptError):
        manager.load()


def test_save_does_not_mutate_passed_session(tmp_path: Path) -> None:
    """save 不得修改调用方传入的 Session 对象。"""
    manager = make_manager(tmp_path)
    session = Session(
        key=DEFAULT_SESSION_KEY,
        messages=make_messages(3),
        summary="已有摘要",
        summary_boundary=1,
        summary_checkpoint_id="cp-save",
    )
    before = deepcopy(session)

    manager.save(session)

    assert session == before
    assert session.messages == before.messages


def test_clear_clears_messages_and_summary_fields(tmp_path: Path) -> None:
    """clear 必须同时清空 messages 与 Summary Checkpoint。"""
    manager = make_manager(tmp_path)
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=make_messages(2),
            summary="待清空摘要",
            summary_boundary=2,
            summary_checkpoint_id="cp-clear",
        )
    )
    manager.clear()

    restored = manager.load()
    assert restored.messages == []
    assert restored.summary == ""
    assert restored.summary_boundary == 0
    assert restored.summary_checkpoint_id == ""

    payload = json.loads(manager.path.read_text(encoding="utf-8"))
    assert payload["summary"] == ""
    assert payload["summary_boundary"] == 0
    assert payload["summary_checkpoint_id"] == ""


def test_last_active_time_returns_none_when_missing(tmp_path: Path) -> None:
    """Session 文件不存在时 last_active_time 返回 None。"""
    manager = make_manager(tmp_path)
    assert manager.last_active_time() is None


def test_last_active_time_returns_file_mtime(tmp_path: Path) -> None:
    """last_active_time 返回 Session 文件的 mtime（本地时间）。"""
    manager = make_manager(tmp_path)
    manager.save(Session(key=DEFAULT_SESSION_KEY, messages=make_messages(1)))

    timestamp = datetime(2024, 1, 2, 3, 4, 5).timestamp()
    os.utime(manager.path, (timestamp, timestamp))

    assert manager.last_active_time() == datetime.fromtimestamp(timestamp)
