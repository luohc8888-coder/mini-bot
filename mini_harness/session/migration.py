"""显式迁移旧 Session；正常加载路径只接受当前 schema。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .manager import SCHEMA_VERSION, SessionManager, SessionStorageError


@dataclass(frozen=True, slots=True)
class SessionMigrationResult:
    status: str
    backup_path: Path | None = None


def migrate_session(manager: SessionManager) -> SessionMigrationResult:
    """持锁校验 v1、保存原始字节备份，再原子写入 v2；重复执行无副作用。"""
    with manager.lock():
        if not manager.path.exists():
            return SessionMigrationResult("missing")
        try:
            original = manager.path.read_bytes()
            payload = json.loads(original)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionStorageError("无法读取待迁移的 Session，原文件未修改") from exc
        if not isinstance(payload, dict):
            raise SessionStorageError("待迁移 Session 顶层必须是对象")
        version = payload.get("schema_version")
        if type(version) is int and version == SCHEMA_VERSION:
            manager.decode_payload(payload)
            return SessionMigrationResult("up_to_date")
        if type(version) is not int or version != 1:
            raise SessionStorageError(f"不支持迁移 Session schema_version={version!r}")
        upgraded = dict(payload)
        upgraded["schema_version"] = SCHEMA_VERSION
        upgraded.setdefault("summary", "")
        upgraded.setdefault("summary_boundary", 0)
        upgraded.setdefault("summary_checkpoint_id", "")
        session = manager.decode_payload(upgraded)

        backup = manager.path.with_name("session.json.v1.bak")
        created = False
        try:
            if backup.exists() or backup.is_symlink():
                if backup.is_symlink() or backup.read_bytes() != original:
                    raise SessionStorageError(f"已有不同的迁移备份，拒绝覆盖：{backup}")
            else:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                with os.fdopen(fd, "wb") as handle:
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            if created:
                backup.unlink(missing_ok=True)
            raise SessionStorageError("Session 备份失败，原文件未修改") from exc
        # 保存失败仍保留完整备份；下次可验证备份一致后重试。
        manager.save(session)
        return SessionMigrationResult("migrated", backup)
