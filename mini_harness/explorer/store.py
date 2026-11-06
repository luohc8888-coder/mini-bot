"""Explorer 索引的本地持久化。

索引目录与 Session/Memory 完全分离：

    ~/.mini-harness/explorer/<workspace_id>/index.json
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mini_harness.workspace import Workspace

from .models import ProjectIndex, SCHEMA_VERSION

DEFAULT_INDEX_ROOT = Path.home() / ".mini-harness" / "explorer"


class IndexStoreError(ValueError):
    """索引读取、校验或写入失败。"""


class IndexStore:
    """负责 Workspace 对应索引的原子保存与读取。"""

    def __init__(
        self,
        workspace: Workspace,
        *,
        base_dir: str | Path | None = None,
    ) -> None:
        self.workspace = workspace
        self.base_dir = (
            Path(base_dir).expanduser()
            if base_dir is not None
            else DEFAULT_INDEX_ROOT
        )

    @property
    def index_path(self) -> Path:
        """当前 Workspace 的 index.json 路径。"""
        return self.base_dir / self.workspace.id / "index.json"

    def exists(self) -> bool:
        """索引文件是否存在。"""
        return self.index_path.is_file()

    def save(self, index: ProjectIndex) -> Path:
        """原子写入项目索引，返回实际路径。"""
        if index.workspace_id != self.workspace.id:
            raise IndexStoreError(
                "索引 workspace_id 与当前 Workspace 不匹配："
                f"{index.workspace_id!r} != {self.workspace.id!r}"
            )
        if index.schema_version != SCHEMA_VERSION:
            raise IndexStoreError(
                f"不支持的索引 schemaVersion：{index.schema_version}"
            )

        payload = json.dumps(
            index.to_dict(),
            ensure_ascii=False,
            indent=2,
        )
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.index_path, payload + "\n")
        return self.index_path

    def load(self) -> ProjectIndex | None:
        """读取索引；文件不存在时返回 None。"""
        if not self.index_path.is_file():
            return None
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IndexStoreError(f"无法读取索引：{exc}") from exc
        if not isinstance(data, dict):
            raise IndexStoreError("索引根节点必须是 JSON 对象")
        schema_version = data.get("schemaVersion")
        if schema_version != SCHEMA_VERSION:
            raise IndexStoreError(
                f"不支持的索引 schemaVersion：{schema_version!r}"
            )
        try:
            return ProjectIndex.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise IndexStoreError(f"索引结构不完整：{exc}") from exc


def _atomic_write_text(target: Path, content: str) -> None:
    """用同目录临时文件 + ``os.replace`` 原子写入文本。"""
    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


__all__ = ["DEFAULT_INDEX_ROOT", "IndexStore", "IndexStoreError"]
