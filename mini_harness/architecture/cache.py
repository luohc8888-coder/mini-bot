"""Phase 4.3：ArchitectureGraph 本地缓存。

缓存只保存已经通过 ``ArchitectureGraphValidator`` 校验的真实 LLM 结果，
并以 CodeGraph 内容的 SHA-256 作为失效键。源码变化后同一工作区的缓存
默认不会继续被使用；用户显式选择时可以读取旧结果，但会返回 ``stale``
状态，且不会被当成当前源码或 Mock 数据展示。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_harness.explorer import CodeGraph

from .models import ArchitectureGraph

_CACHE_SCHEMA_VERSION = 1
_WORKSPACE_ID_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class ArchitectureCacheError(RuntimeError):
    """缓存读写失败；调用方可以降级为重新分析，不应崩溃。"""


@dataclass(frozen=True, slots=True)
class ArchitectureCacheInfo:
    """缓存的只读元信息。"""

    path: Path
    available: bool
    status: str = "missing"
    generated_at: str = ""
    fingerprint: str = ""
    stored_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "available": self.available,
            "status": self.status,
            "generatedAt": self.generated_at,
            "fingerprint": self.fingerprint,
            "storedFingerprint": self.stored_fingerprint,
        }


def codegraph_fingerprint(graph: CodeGraph) -> str:
    """计算 CodeGraph 的稳定内容指纹，用于缓存失效。"""
    payload = graph.to_dict()
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_architecture_cache_dir(workspace_id: str) -> Path:
    """返回某个 Workspace 的默认缓存目录。"""
    explicit = os.getenv("MINI_HARNESS_ARCHITECTURE_HOME")
    if explicit:
        base = Path(explicit).expanduser()
    else:
        home = os.getenv("MINI_HARNESS_HOME")
        base = Path(home).expanduser() if home else Path.home() / ".mini-harness"
        base = base / "architecture"
    safe_id = _WORKSPACE_ID_SAFE.sub("_", workspace_id).strip("._") or "workspace"
    return base / safe_id


class ArchitectureCache:
    """磁盘上的 ArchitectureGraph 缓存。

    Args:
        path: 缓存 JSON 文件路径。通常用 ``ArchitectureCache.for_workspace``
            生成；测试可以注入 ``tmp_path / "architecture.json"``。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    @classmethod
    def for_workspace(
        cls,
        workspace_id: str,
        *,
        base_dir: Path | str | None = None,
    ) -> "ArchitectureCache":
        if base_dir is not None:
            safe_id = _WORKSPACE_ID_SAFE.sub("_", workspace_id).strip("._") or "workspace"
            return cls(Path(base_dir).expanduser() / safe_id / "architecture.json")
        return cls(default_architecture_cache_dir(workspace_id) / "architecture.json")

    def _read_payload(self) -> dict[str, Any] | None:
        """读取缓存 JSON；文件不存在或损坏时返回 None。"""
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schemaVersion") != _CACHE_SCHEMA_VERSION:
            return None
        return payload

    def _graph_from_payload(self, payload: dict[str, Any]) -> ArchitectureGraph | None:
        raw_graph = payload.get("graph")
        if not isinstance(raw_graph, dict):
            return None
        try:
            graph = ArchitectureGraph.from_dict(raw_graph)
        except Exception:
            return None
        if graph.mock:
            return None
        return graph

    def load(self, expected_fingerprint: str) -> ArchitectureGraph | None:
        """读取并校验缓存；不存在、损坏或源码已变化时返回 None。"""
        payload = self._read_payload()
        if payload is None:
            return None
        if str(payload.get("fingerprint", "")) != expected_fingerprint:
            return None
        return self._graph_from_payload(payload)

    def load_stale(self) -> ArchitectureGraph | None:
        """读取旧缓存，不校验源码指纹；仅供用户显式查看旧结果。"""
        payload = self._read_payload()
        if payload is None:
            return None
        return self._graph_from_payload(payload)

    def save(self, graph: ArchitectureGraph, fingerprint: str) -> None:
        """原子写入缓存；失败时抛 ArchitectureCacheError。"""
        if graph.mock:
            raise ArchitectureCacheError("拒绝缓存 Mock ArchitectureGraph")
        payload = {
            "schemaVersion": _CACHE_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "savedAt": datetime.now(timezone.utc).isoformat(),
            "graph": graph.to_dict(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(self.path)
        except OSError as exc:
            raise ArchitectureCacheError(f"写入架构缓存失败：{exc}") from exc

    def info(self, expected_fingerprint: str) -> ArchitectureCacheInfo:
        """读取缓存状态；不会抛出损坏文件异常。

        ``status`` 取值：
        - ``valid``：缓存存在且源码指纹匹配；
        - ``stale``：缓存存在但源码指纹已变化；
        - ``missing``：缓存不存在、损坏或无法解析。
        """
        payload = self._read_payload()
        if payload is None:
            return ArchitectureCacheInfo(path=self.path, available=False)
        graph = self._graph_from_payload(payload)
        if graph is None:
            return ArchitectureCacheInfo(path=self.path, available=False)
        stored_fingerprint = str(payload.get("fingerprint", ""))
        status = (
            "valid" if stored_fingerprint == expected_fingerprint else "stale"
        )
        return ArchitectureCacheInfo(
            path=self.path,
            available=status == "valid",
            status=status,
            generated_at=graph.generated_at,
            fingerprint=expected_fingerprint,
            stored_fingerprint=stored_fingerprint,
        )

    def clear(self) -> None:
        """删除缓存文件；不存在时忽略。"""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise ArchitectureCacheError(f"删除架构缓存失败：{exc}") from exc


__all__ = [
    "ArchitectureCache",
    "ArchitectureCacheError",
    "ArchitectureCacheInfo",
    "codegraph_fingerprint",
    "default_architecture_cache_dir",
]
