"""Mini Harness CLI 兼容入口。

推荐安装后直接使用 ``mini-bot`` 命令；仍保留此文件以兼容：

    uv run python main.py
    uv run python main.py --demo
    uv run python main.py --list-providers
"""

from __future__ import annotations

import asyncio
import sys

# 重新导出给测试和旧脚本使用。
from mini_harness.cli.main import (
    build_demo_provider,
    cli,
    main,
    parse_args,
    run_demo,
)

__all__ = [
    "build_demo_provider",
    "cli",
    "main",
    "parse_args",
    "run_demo",
]


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
