"""文件工具共用的大小限制与原子写入；不依赖具体 Tool 类。"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

_MAX_READ_BYTES = 2 * 1024 * 1024
_MAX_EDIT_BYTES = 2 * 1024 * 1024


def _atomic_write_text(target: Path, content: str) -> None:
    """用同目录临时文件 + ``os.replace`` 原子写入文本。

    Nanobot 的 write_file 直接 ``Path.write_text``；这里保留相同的工具语义，
    但增加原子替换，避免进程异常时留下半个文件。已有文件会保留原权限位。
    """
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        pass

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

        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
