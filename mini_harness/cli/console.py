"""终端输入输出、流式渲染与 Provider 目录展示。"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from mini_harness.providers.presets import ProviderPreset, grouped_presets


class Console:
    """终端输入输出封装；测试可替换为脚本化实现。

    颜色只在交互式终端启用；重定向输出或设置
    ``MINI_HARNESS_NO_COLOR=1`` 时自动关闭。
    """

    _USER_COLOR = "1;36"
    _ASSISTANT_COLOR = "1;32"
    _TOOL_COLOR = "33"
    _ERROR_COLOR = "1;31"

    def __init__(self, *, color: bool | None = None) -> None:
        self.color = (
            color
            if color is not None
            else bool(
                sys.stdout.isatty()
                and not os.getenv("MINI_HARNESS_NO_COLOR")
            )
        )

    def _style(self, text: str, code: str) -> str:
        """按需给文本加 ANSI 颜色；非 TTY 返回原文。"""
        if not getattr(self, "color", False):
            return text
        return f"\033[{code}m{text}\033[0m"

    def user_prompt(self) -> str:
        """返回带颜色的用户输入提示符。"""
        return self._style("你 > ", self._USER_COLOR)

    def write(self, text: str = "") -> None:
        """输出一行文本。"""
        print(text)

    def write_assistant(self, text: str) -> None:
        """输出助手消息，角色标签使用绿色，并对多行内容做对齐缩进。"""
        lines = text.strip("\n").splitlines() or [""]
        label = self._style("助手 >", self._ASSISTANT_COLOR)
        formatted = f"{label} {lines[0]}"
        if len(lines) > 1:
            padding = " " * len("助手 > ")
            formatted += "\n" + "\n".join(
                f"{padding}{line}" for line in lines[1:]
            )
        self.write(formatted)

    def write_stream_start(self) -> None:
        """开始一条流式助手消息，只打印一次角色标签。"""
        label = self._style("助手 >", self._ASSISTANT_COLOR)
        print(f"{label} ", end="", flush=True)

    def write_stream_delta(self, text: str) -> None:
        """打印一个流式文本片段，不自动换行。"""
        print(text, end="", flush=True)

    def write_stream_end(self) -> None:
        """结束流式助手消息并换行。"""
        print()

    def write_tool(self, text: str) -> None:
        """输出工具执行提示，使用黄色。"""
        self.write(f"{self._style('[工具]', self._TOOL_COLOR)} {text}")

    def write_error(self, text: str) -> None:
        """输出错误信息，使用红色。"""
        self.write(self._style(text, self._ERROR_COLOR))

    async def read(self, prompt: str = "") -> str:
        """异步读取一行普通输入。

        ``input()`` 是阻塞调用，使用 ``asyncio.to_thread`` 放到线程中，
        避免卡住正在运行的 asyncio 事件循环。

        带 ANSI 颜色的提示符不能直接交给 ``input()``：readline 会误把
        转义序列计入可见宽度，导致退格/删除键行为异常。此时先打印提示符，
        再用空 prompt 读取输入。
        """
        if "\x1b[" in prompt:
            print(prompt, end="", flush=True)
            prompt = ""
        return (await asyncio.to_thread(input, prompt)).strip()

    async def read_secret(self, prompt: str = "") -> str:
        """异步读取 API Key，终端输入不回显。

        部分终端会在 getpass 返回值中带回 ``\\r``/``\\n``；这里显式清理，
        避免换行符被当作 API Key 的一部分保存到配置中。
        """
        raw = await asyncio.to_thread(getpass.getpass, prompt)
        return raw.replace("\r", "").replace("\n", "").strip()


class StreamingAssistantRenderer:
    """把 Provider 的文本增量渲染成一条连续的助手消息。"""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.started = False
        self.has_output = False
        self.tools_printed = False

    def on_delta(self, text: str) -> None:
        """收到一个文本片段；第一次收到时打印角色标签。"""
        if not text:
            return
        if not self.started:
            self.console.write_stream_start()
            self.started = True
        self.console.write_stream_delta(text)
        self.has_output = True

    def on_tool_start(self, name: str) -> None:
        """在工具执行前结束当前文本行并打印工具提示。"""
        self.finish()
        self.console.write_tool(name)
        self.tools_printed = True

    def finish(self) -> None:
        """结束当前流式消息；没有输出时不做任何事。"""
        if self.started:
            self.console.write_stream_end()
            self.started = False


def _format_base_url(preset: ProviderPreset) -> str:
    """目录展示用 Base URL 文案。"""
    if preset.default_base_url:
        return preset.default_base_url
    if preset.requires_base_url:
        return "（需在 CLI 中输入）"
    return "无需"


def print_provider_catalog(console: Console | None = None) -> None:
    """打印内置 Provider 目录，供 ``--list-providers`` 使用。"""
    output = console or Console()
    output.write("可用 Provider：")
    for group_name, presets in grouped_presets():
        output.write(f"\n[{group_name}]")
        for preset in presets:
            env_hint = (
                "环境变量: " + " / ".join(preset.env_keys)
                if preset.env_keys
                else "无需 API Key"
            )
            output.write(
                f"  - {preset.display_name} (key={preset.key})\n"
                f"      默认模型: {preset.default_model or '需手动输入'}\n"
                f"      Base URL: {_format_base_url(preset)}\n"
                f"      {env_hint}"
            )
