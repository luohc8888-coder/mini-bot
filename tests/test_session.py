"""Session 数据结构、JSON 持久化与 Workspace 隔离测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mini_harness.session.manager as manager_module
from mini_harness.session import (
    DEFAULT_SESSION_KEY,
    Session,
    SessionCorruptError,
    SessionInUseError,
    SessionManager,
    SessionSerializationError,
    SessionStorageError,
    SessionWorkspaceMismatch,
)


def make_workspace(tmp_path: Path, name: str = "workspace") -> Path:
    """创建测试 Workspace。"""
    workspace = tmp_path / name
    workspace.mkdir()
    return workspace


def test_missing_session_returns_empty_without_creating_file(
    tmp_path: Path,
) -> None:
    """文件不存在时返回空 Session，且不提前创建状态文件。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    session = manager.load()
    assert session.key == DEFAULT_SESSION_KEY
    assert session.messages == []
    assert not manager.path.exists()


def test_save_and_reload_restores_history(tmp_path: Path) -> None:
    """同一 Workspace 的新 SessionManager 可以恢复相同历史。"""
    workspace = make_workspace(tmp_path)
    data_root = tmp_path / "data"
    first = SessionManager(workspace, data_root=data_root)
    session = Session(
        key=DEFAULT_SESSION_KEY,
        messages=[
            {"role": "user", "content": "计算 2 + 3"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_001",
                        "name": "add",
                        "arguments": {"a": 2, "b": 3},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_001",
                "name": "add",
                "content": "5",
            },
            {"role": "assistant", "content": "结果是 5"},
        ],
    )
    first.save(session)

    assert first.path.exists()
    assert json.loads(first.path.read_text(encoding="utf-8"))["schema_version"] == 2

    second = SessionManager(workspace, data_root=data_root)
    restored = second.load()
    assert restored == session


def test_load_returns_independent_copy(tmp_path: Path) -> None:
    """修改 load 返回的对象不能影响磁盘上的历史。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=[{"role": "user", "content": "原始消息"}],
        )
    )

    loaded = manager.load()
    loaded.messages.append({"role": "user", "content": "外部添加"})
    loaded.messages[0]["content"] = "被修改"

    assert manager.load().messages == [{"role": "user", "content": "原始消息"}]


def test_clear_updates_disk_and_memory(tmp_path: Path) -> None:
    """clear 后重新加载必须为空。"""
    workspace = make_workspace(tmp_path)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    manager.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=[{"role": "user", "content": "要清空"}],
        )
    )
    manager.clear()
    assert manager.load().messages == []

    fresh = SessionManager(workspace, data_root=tmp_path / "data")
    assert fresh.load().messages == []


def test_different_workspaces_are_isolated(tmp_path: Path) -> None:
    """不同 Workspace 的 Session 文件路径和历史互不相通。"""
    data_root = tmp_path / "data"
    first_ws = make_workspace(tmp_path, "first")
    second_ws = make_workspace(tmp_path, "second")
    first = SessionManager(first_ws, data_root=data_root)
    second = SessionManager(second_ws, data_root=data_root)

    first.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=[{"role": "user", "content": "first history"}],
        )
    )
    assert second.path != first.path
    assert second.load().messages == []

    second.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=[{"role": "user", "content": "second history"}],
        )
    )
    assert first.load().messages == [{"role": "user", "content": "first history"}]
    assert second.load().messages == [{"role": "user", "content": "second history"}]


def test_corrupt_json_raises_and_preserves_file(tmp_path: Path) -> None:
    """损坏的 JSON 必须明确报错，绝不静默覆盖原文件。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    invalid = "{ this is not valid json"
    manager.path.write_text(invalid, encoding="utf-8")

    with pytest.raises(SessionCorruptError):
        manager.load()
    assert manager.path.read_text(encoding="utf-8") == invalid


def test_workspace_mismatch_is_rejected(tmp_path: Path) -> None:
    """同一存储目录下 Workspace 记录不匹配时拒绝恢复。"""
    first_ws = make_workspace(tmp_path, "first")
    second_ws = make_workspace(tmp_path, "second")
    data_root = tmp_path / "data"
    first = SessionManager(first_ws, data_root=data_root)
    second = SessionManager(second_ws, data_root=data_root)
    first.save(
        Session(
            key=DEFAULT_SESSION_KEY,
            messages=[{"role": "user", "content": "first"}],
        )
    )
    # 把第一个 Workspace 的文件复制到第二个 Workspace 的路径下。
    second.path.parent.mkdir(parents=True, exist_ok=True)
    second.path.write_text(first.path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(SessionWorkspaceMismatch):
        second.load()


def test_save_failure_keeps_previous_file_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原子保存失败时旧 Session 文件不被破坏，临时文件被清理。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    old = Session(
        key=DEFAULT_SESSION_KEY,
        messages=[{"role": "user", "content": "旧历史"}],
    )
    manager.save(old)
    old_text = manager.path.read_text(encoding="utf-8")

    real_replace = manager_module.os.replace

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(manager_module.os, "replace", failing_replace)
    with pytest.raises(SessionStorageError):
        manager.save(
            Session(
                key=DEFAULT_SESSION_KEY,
                messages=[{"role": "user", "content": "新历史"}],
            )
        )

    monkeypatch.setattr(manager_module.os, "replace", real_replace)
    assert manager.path.read_text(encoding="utf-8") == old_text
    assert list(manager.path.parent.glob("*.tmp")) == []


def test_unserializable_message_raises_without_writing(tmp_path: Path) -> None:
    """不可 JSON 序列化的消息不能被保存。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    session = Session(
        key=DEFAULT_SESSION_KEY,
        messages=[{"role": "user", "content": {1, 2, 3}}],  # type: ignore[dict-item]
    )
    with pytest.raises(SessionSerializationError):
        manager.save(session)
    assert not manager.path.exists()


def test_tool_call_fields_survive_json_roundtrip(tmp_path: Path) -> None:
    """assistant tool_calls 与 tool_call_id 必须完整保存和恢复。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    messages = [
        {"role": "user", "content": "计算 2 + 3"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_001",
                    "name": "add",
                    "arguments": {"a": 2, "b": 3},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_001",
            "name": "add",
            "content": "5",
        },
        {"role": "assistant", "content": "结果是 5"},
    ]
    manager.save(Session(key=DEFAULT_SESSION_KEY, messages=messages))
    restored = manager.load().messages
    assert restored[1]["tool_calls"][0]["id"] == "call_001"
    assert restored[2]["tool_call_id"] == "call_001"
    assert restored[2]["content"] == "5"


def test_session_lock_rejects_second_cli_instance(tmp_path: Path) -> None:
    """同一 Workspace 的第二个 CLI 锁必须收到明确并发错误。"""
    manager = SessionManager(make_workspace(tmp_path), data_root=tmp_path / "data")
    second = SessionManager(manager.workspace, data_root=tmp_path / "data")
    with manager.lock():
        with pytest.raises(SessionInUseError):
            with second.lock():
                pass
