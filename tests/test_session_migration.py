"""Session 显式迁移：原始备份、失败恢复、严格加载与 CLI 路径。"""

import json
import os
import stat
from pathlib import Path

import pytest

from mini_harness.cli.main import main, parse_args
from mini_harness.session import (
    SessionCorruptError,
    SessionInUseError,
    SessionManager,
    SessionStorageError,
)
from mini_harness.session.migration import migrate_session


def make_legacy(tmp_path: Path, *, with_summary: bool = True) -> SessionManager:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    payload = {
        "schema_version": 1,
        "workspace": str(workspace),
        "session_key": manager.session_key,
        "messages": [
            {"role": "user", "content": "保留精确历史。"},
            {"role": "assistant", "content": "已记录。"},
        ],
    }
    if with_summary:
        payload.update(summary="原有摘要。", summary_boundary=2, summary_checkpoint_id="old-cp")
    manager.path.parent.mkdir(parents=True)
    manager.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return manager


@pytest.mark.parametrize("with_summary", [True, False])
def test_migration_preserves_history_checkpoint_and_original_backup(
    tmp_path: Path, with_summary: bool,
) -> None:
    manager = make_legacy(tmp_path, with_summary=with_summary)
    before = manager.path.read_bytes()
    payload = json.loads(before)
    result = migrate_session(manager)
    assert result.status == "migrated"
    assert result.backup_path.read_bytes() == before
    assert stat.S_IMODE(result.backup_path.stat().st_mode) == 0o600
    session = manager.load()
    assert session.messages == payload["messages"]
    assert session.summary == payload.get("summary", "")
    assert session.summary_boundary == payload.get("summary_boundary", 0)
    assert session.summary_checkpoint_id == payload.get("summary_checkpoint_id", "")
    saved = manager.path.read_bytes()
    timestamp = manager.path.stat().st_mtime_ns
    assert migrate_session(manager).status == "up_to_date"
    assert manager.path.read_bytes() == saved
    assert manager.path.stat().st_mtime_ns == timestamp


def test_failed_session_save_keeps_original_and_retryable_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_legacy(tmp_path)
    before = manager.path.read_bytes()
    save = manager.save

    def fail_save(*args):
        raise SessionStorageError("disk full")

    monkeypatch.setattr(manager, "save", fail_save)
    with pytest.raises(SessionStorageError, match="disk full"):
        migrate_session(manager)
    assert manager.path.read_bytes() == before
    assert manager.path.with_name("session.json.v1.bak").read_bytes() == before
    monkeypatch.setattr(manager, "save", save)
    assert migrate_session(manager).status == "migrated"


@pytest.mark.parametrize("kind", ["different", "symlink"])
def test_conflicting_backup_is_never_overwritten(tmp_path: Path, kind: str) -> None:
    manager = make_legacy(tmp_path)
    before = manager.path.read_bytes()
    backup = manager.path.with_name("session.json.v1.bak")
    original_backup = b"unrelated data"
    if kind == "different":
        backup.write_bytes(original_backup)
    else:
        external = tmp_path / "external"
        external.write_bytes(original_backup)
        backup.symlink_to(external)
    with pytest.raises(SessionStorageError, match="拒绝覆盖"):
        migrate_session(manager)
    assert backup.read_bytes() == original_backup
    assert manager.path.read_bytes() == before


def test_backup_failure_does_not_change_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_legacy(tmp_path)
    before = manager.path.read_bytes()

    def fail_fsync(fd):
        raise OSError("backup fsync failed")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(SessionStorageError, match="备份失败"):
        migrate_session(manager)
    assert manager.path.read_bytes() == before
    assert not manager.path.with_name("session.json.v1.bak").exists()


@pytest.mark.parametrize("invalid", ["json", "workspace", "boundary", "future", "bool"])
def test_invalid_legacy_data_fails_before_backup(tmp_path: Path, invalid: str) -> None:
    manager = make_legacy(tmp_path)
    payload = json.loads(manager.path.read_bytes())
    if invalid == "workspace":
        payload["workspace"] = str(tmp_path)
    elif invalid == "boundary":
        payload["summary_boundary"] = 100
    elif invalid == "future":
        payload["schema_version"] = 100
    elif invalid == "bool":
        payload["schema_version"] = True
    manager.path.write_text("{" if invalid == "json" else json.dumps(payload))
    before = manager.path.read_bytes()
    with pytest.raises(SessionStorageError):
        migrate_session(manager)
    assert manager.path.read_bytes() == before
    assert not manager.path.with_name("session.json.v1.bak").exists()


def test_migration_respects_running_session_lock(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")
    manager = make_legacy(tmp_path)
    before = manager.path.read_bytes()
    with manager.lock():
        with pytest.raises(SessionInUseError):
            migrate_session(manager)
    assert manager.path.read_bytes() == before


@pytest.mark.parametrize("field", ["summary", "summary_boundary", "summary_checkpoint_id"])
def test_current_schema_requires_checkpoint_fields(tmp_path: Path, field: str) -> None:
    manager = make_legacy(tmp_path)
    migrate_session(manager)
    payload = json.loads(manager.path.read_bytes())
    payload.pop(field)
    manager.path.write_text(json.dumps(payload))
    before = manager.path.read_bytes()
    with pytest.raises(SessionCorruptError, match="缺少字段"):
        manager.load()
    assert manager.path.read_bytes() == before


async def test_migration_cli_uses_workspace_and_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    manager = make_legacy(tmp_path)
    monkeypatch.setenv("MINI_HARNESS_HOME", str(manager.data_root))
    await main(["--migrate-session", "--workspace", str(manager.workspace)])
    assert "已迁移" in capsys.readouterr().out
    assert manager.load().summary == "原有摘要。"
    await main(["--migrate-session", "--workspace", str(manager.workspace)])
    assert "无需迁移" in capsys.readouterr().out


async def test_migration_cli_failure_is_nonzero_and_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    manager = make_legacy(tmp_path)
    manager.path.write_text("{invalid}")
    monkeypatch.setenv("MINI_HARNESS_HOME", str(manager.data_root))
    with pytest.raises(SystemExit) as error:
        await main(["--migrate-session", "--workspace", str(manager.workspace)])
    assert error.value.code == 1
    assert "迁移失败" in capsys.readouterr().err
    assert manager.path.read_text() == "{invalid}"


def test_migration_and_demo_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--migrate-session", "--demo"])
