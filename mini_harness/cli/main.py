"""Mini Harness CLI 的可安装入口。

安装后可以在任意目录直接运行：

    mini-bot
    mini-bot --demo
    mini-bot --list-providers

也支持作为模块运行：

    python -m mini_harness
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from ..agent import AgentLoop, AgentRunner
from ..bus import InboundMessage, MessageBus
from ..providers import create_mock_provider
from ..tools import AddTool, ToolRegistry
from . import print_provider_catalog, run_interactive_cli


def build_demo_provider():
    """构造离线演示 Provider。

    Returns:
        MockProvider：第一次返回 add(2, 3) 工具调用，第二次根据 tool 结果
        返回 ``"结果是 5"``。
    """
    return create_mock_provider()


async def run_demo() -> None:
    """运行一次完整的 V0 工具调用闭环并打印最终答案。"""
    bus = MessageBus()

    registry = ToolRegistry()
    registry.register(AddTool())

    provider = build_demo_provider()
    runner = AgentRunner(provider=provider, tools=registry)
    agent_loop = AgentLoop(bus=bus, runner=runner, max_iterations=8)

    # AgentLoop.run 是常驻循环，用 create_task 并发启动，主协程才能继续
    # 投递入站消息并等待出站结果。
    loop_task = asyncio.create_task(agent_loop.run())
    try:
        await bus.publish_inbound(
            InboundMessage(content="计算 2 + 3", sender="cli")
        )
        outbound = await asyncio.wait_for(
            bus.consume_outbound(),
            timeout=5.0,
        )
        print(outbound.content)
    finally:
        agent_loop.stop()
        await loop_task


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(
        prog="mini-bot",
        description="Mini Harness Agent CLI",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--demo",
        action="store_true",
        help="运行一次离线 MockProvider 工具调用演示",
    )
    mode.add_argument(
        "--interactive",
        action="store_true",
        help="强制进入交互式 CLI",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="列出所有内置 Provider 预设后退出",
    )
    return parser.parse_args(list(argv))


async def main(argv: Sequence[str] | None = None) -> None:
    """CLI 主入口。

    无参数时：终端环境进入交互式 CLI，非交互环境（如测试/管道）运行
    ``--demo``，避免自动化环境因为等待输入而挂起。
    """
    args = parse_args([] if argv is None else argv)

    if args.list_providers:
        print_provider_catalog()
        return

    if args.demo:
        await run_demo()
        return

    if args.interactive or sys.stdin.isatty():
        await run_interactive_cli()
        return

    await run_demo()


def cli() -> None:
    """同步控制台脚本入口，供 ``[project.scripts]`` 调用。"""
    asyncio.run(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
