"""可安装 CLI 入口的默认行为测试。"""

from __future__ import annotations

import asyncio

import mini_harness.cli.main as cli_main_module


def test_mini_bot_without_args_enters_interactive(
    monkeypatch,
) -> None:
    """`mini-bot` 不带参数时默认进入 CLI，不因非 TTY 退回 demo。"""
    calls: list[dict[str, object]] = []

    async def fake_run_interactive_cli(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        cli_main_module,
        "run_interactive_cli",
        fake_run_interactive_cli,
    )
    monkeypatch.setattr(
        cli_main_module.sys.stdin,
        "isatty",
        lambda: False,
    )

    asyncio.run(cli_main_module.main([]))

    assert len(calls) == 1
    assert calls[0]["workspace"] is None


def test_mini_bot_demo_still_runs_explicit_demo(monkeypatch) -> None:
    """`--demo` 仍保持一次性离线演示，不进入交互循环。"""
    calls: list[str] = []

    async def fake_run_demo():
        calls.append("demo")

    async def fake_run_interactive_cli(**kwargs):
        calls.append("interactive")

    monkeypatch.setattr(cli_main_module, "run_demo", fake_run_demo)
    monkeypatch.setattr(
        cli_main_module,
        "run_interactive_cli",
        fake_run_interactive_cli,
    )

    asyncio.run(cli_main_module.main(["--demo"]))

    assert calls == ["demo"]
