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
from ..workspace import WorkspaceError
from ..tools import AddTool, ToolRegistry
from .console import print_provider_catalog
from .app import run_interactive_cli


def build_demo_provider():
    """构造离线演示 Provider。

    Returns:
        MockProvider：第一次返回 add(2, 3) 工具调用，第二次根据 tool 结果
        返回 ``"结果是 5"``。
    """
    return create_mock_provider()


async def run_demo() -> None:
    """运行一次显式无状态的离线工具调用演示并打印最终答案。"""
    bus = MessageBus()

    registry = ToolRegistry()
    registry.register(AddTool())

    provider = build_demo_provider()
    runner = AgentRunner(provider=provider, tools=registry)
    agent_loop = AgentLoop(stateless=True, bus=bus, runner=runner, max_iterations=32)

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
        help="兼容旧用法；不带参数运行 mini-bot 时默认进入交互式 CLI",
    )
    mode.add_argument(
        "--migrate-session", action="store_true",
        help="显式迁移当前 Workspace 的旧 Session，保留原始备份后退出",
    )
    mode.add_argument(
        "--list-providers",
        action="store_true",
        help="列出所有内置 Provider 预设后退出",
    )
    parser.add_argument(
        "--workspace",
        metavar="PATH",
        default=None,
        help="指定 Workspace；默认使用当前工作目录",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=_positive_output_tokens,
        default=None,
        metavar="N",
        help="本次最大输出 Token（覆盖已保存配置；新建 profile 时保存）",
    )
    parser.add_argument(
        "--context-window-tokens",
        type=int,
        default=0,
        metavar="N",
        help="本次模型上下文窗口 Token（覆盖已保存配置；新建 profile 时保存）",
    )
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="本次请求启用 thinking/reasoning 模式（也覆盖已保存配置）",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="",
        metavar="LEVEL",
        help="本次请求的推理档位（如 high/max，覆盖已保存配置）",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=32,
        metavar="N",
        help="单次 Agent 运行的最大模型/工具迭代次数，默认 32",
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=0,
        metavar="N",
        help="显式覆盖模型上下文窗口；0 表示使用 profile/默认值",
    )
    parser.add_argument(
        "--reserve-output-tokens",
        type=int,
        default=0,
        metavar="N",
        help="显式覆盖输出预留；0 表示使用模型配置",
    )
    parser.add_argument(
        "--idle-compact-after-minutes",
        type=float,
        default=None,
        metavar="N",
        help="Session 空闲多少分钟后后台压缩；0 关闭 Idle Compaction（默认 15）",
    )
    parser.add_argument(
        "--dream-interval-hours",
        type=float,
        default=None,
        metavar="N",
        help="自动 Dream 检查间隔小时数（默认 2）",
    )
    parser.add_argument(
        "--no-dream",
        action="store_true",
        help="本次运行关闭自动 Dream（/dream 手动整理仍可用）",
    )
    stream_mode = parser.add_mutually_exclusive_group()
    stream_mode.add_argument(
        "--stream",
        action="store_true",
        dest="stream",
        help="启用流式输出（默认开启）",
    )
    stream_mode.add_argument(
        "--no-stream",
        action="store_false",
        dest="stream",
        help="关闭流式输出，等待模型完整回答后一次性打印",
    )
    parser.set_defaults(stream=True)
    return parser.parse_args(list(argv))


def _positive_output_tokens(value: str) -> int:
    """参数省略表示不覆盖；显式值必须为正整数。"""
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("输出额度必须是正整数") from exc
    if count <= 0:
        raise argparse.ArgumentTypeError("输出额度必须是正整数")
    return count


async def main(argv: Sequence[str] | None = None) -> None:
    """CLI 主入口。

    无参数时直接进入交互式 CLI，不依赖 stdin 是否为 TTY；需要一次性
    离线演示时显式传入 ``--demo``。
    """
    args = parse_args([] if argv is None else argv)

    if args.list_providers:
        print_provider_catalog()
        return

    if args.demo:
        await run_demo()
        return

    if args.migrate_session:
        from mini_harness.session import SessionError, SessionManager
        from mini_harness.session.migration import migrate_session
        from mini_harness.workspace import WorkspaceResolver

        try:
            manager = SessionManager(WorkspaceResolver().resolve(args.workspace))
            result = migrate_session(manager)
        except (SessionError, WorkspaceError) as exc:
            print(f"Session 迁移失败：{exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        if result.status == "migrated":
            print(f"Session 已迁移；原始备份：{result.backup_path}")
        elif result.status == "up_to_date":
            print("Session 已是当前格式，无需迁移。")
        else:
            print("当前 Workspace 没有 Session，无需迁移。")
        return

    # 默认行为就是进入交互式 CLI；`--interactive` 仅保留为显式兼容参数，
    # `--demo` 才运行一次性离线演示。这样用户直接执行 `mini-bot` 即可进入
    # CLI，不依赖 stdin 是否为 TTY（IDE、管道包装器也能正常进入）。
    try:
        await run_interactive_cli(
            workspace=args.workspace,
            max_iterations=args.max_iterations,
            stream=args.stream,
            max_context_tokens=args.max_context_tokens,
            reserve_output_tokens=args.reserve_output_tokens,
            max_output_tokens=args.max_output_tokens,
            context_window_tokens=args.context_window_tokens,
            thinking=args.thinking,
            reasoning_effort=args.reasoning_effort,
            idle_compact_after_minutes=args.idle_compact_after_minutes,
            dream_interval_hours=args.dream_interval_hours,
            no_dream=args.no_dream,
        )
    except WorkspaceError as exc:
        print(f"Workspace 错误：{exc}", file=sys.stderr)
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)


def cli() -> None:
    """同步控制台脚本入口，供 ``[project.scripts]`` 调用。"""
    asyncio.run(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
