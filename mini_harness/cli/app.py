"""Mini Harness 交互式 CLI。

优先复用默认配置启动；没有默认配置时选择 Provider / 模型 / API Key。支持
保存默认配置、运行中通过 ``/model`` 切换 Provider/模型，以及 V1 的
Workspace 固定、Session 历史恢复、``/history`` 和 ``/clear``。
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

from mini_harness.agent import AgentLoop, AgentRunner, AgentRunSpec
from mini_harness.bus import InboundMessage, MessageBus, OutboundMessage
from mini_harness.providers import (
    LLMProvider,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderError,
    ProviderPreset,
    create_provider,
    get_preset,
    grouped_presets,
)
from mini_harness.providers.base import Message, TextDeltaCallback
from mini_harness.session import (
    SessionError,
    SessionManager,
    WorkspaceResolver,
)
from mini_harness.tools import (
    AddTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    ToolRegistry,
    WriteFileTool,
)

from .profiles import ProfileStore, ProviderProfile


@dataclass(slots=True)
class Selection:
    """CLI 当前选择的 Provider 配置。"""

    config: ProviderConfig
    preset: ProviderPreset


class _AbortSelection(Exception):
    """用户在配置菜单中取消选择。"""


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


class InteractiveCLI:
    """交互式配置菜单与多轮聊天循环。"""

    def __init__(
        self,
        *,
        console: Console | None = None,
        profile_store: ProfileStore | None = None,
        max_iterations: int = 32,
        workspace: str | Path | None = None,
        data_root: Path | None = None,
        session_manager: SessionManager | None = None,
        stream: bool = False,
    ) -> None:
        """初始化 CLI 依赖。

        Args:
            workspace: V1 Workspace；为 None 且未提供 SessionManager 时
                保持 V0 无 Session 的兼容行为。
            data_root: Session 数据根目录，便于测试或自定义 Harness 状态位置。
            session_manager: 可注入的已有 SessionManager，优先于 workspace。
            stream: 是否启用流式输出；``run_interactive_cli`` 默认开启，
                直接实例化时默认关闭以保持旧行为。
        """
        self.console = console or Console()
        self.store = profile_store or ProfileStore()
        self.max_iterations = max_iterations
        self.stream = stream
        self.tools = ToolRegistry()
        self.tools.register(AddTool())

        if session_manager is not None:
            self.session_manager: SessionManager | None = session_manager
            self.workspace: Path | None = session_manager.workspace
        elif workspace is not None or data_root is not None:
            resolved = WorkspaceResolver().resolve(workspace)
            self.session_manager = SessionManager(resolved, data_root=data_root)
            self.workspace = resolved.path
        else:
            # 兼容旧测试和作为库使用的 V0 调用方式；安装后的 CLI
            # ``run_interactive_cli`` 总会显式传入 workspace。
            self.session_manager = None
            self.workspace = None

        # 文件工具必须绑定固定的 Workspace，不能读取进程 cwd。
        if self.workspace is not None:
            file_workspace = WorkspaceResolver().resolve(self.workspace)
            self.tools.register(GrepTool(file_workspace))
            self.tools.register(ListDirTool(file_workspace))
            self.tools.register(ReadFileTool(file_workspace))
            self.tools.register(WriteFileTool(file_workspace))

    async def run(self) -> None:
        """运行配置选择与聊天主循环。"""
        lock_context = (
            self.session_manager.lock()
            if self.session_manager is not None
            else nullcontext()
        )
        try:
            with lock_context:
                try:
                    self._show_banner()
                except SessionError as exc:
                    self.console.write_error(f"Session 加载失败：{exc}")
                    return
                try:
                    while True:
                        try:
                            selection = await self._select_config()
                        except _AbortSelection:
                            self.console.write("已退出。")
                            return

                        if selection is None:
                            self.console.write("已退出。")
                            return

                        try:
                            provider = create_provider(selection.config)
                        except ProviderConfigurationError as exc:
                            self.console.write(f"配置错误：{exc}")
                            continue

                        should_switch = await self._chat_loop(
                            provider,
                            selection,
                        )
                        if not should_switch:
                            self.console.write("已退出。")
                            return
                except (KeyboardInterrupt, EOFError):
                    self.console.write("\n已退出。")
        except SessionError as exc:
            self.console.write_error(str(exc))

    def _show_banner(self) -> None:
        """显示 V1 启动摘要或 V0 兼容欢迎信息。"""
        self.console.write("")
        if self.session_manager is not None:
            session = self.session_manager.load()
            self.console.write("Mini Harness V1")
            self.console.write(f"Workspace: {self.workspace}")
            self.console.write(f"Session: {self.session_manager.session_key}")
            self.console.write(f"History: {len(session.messages)} messages")
            self.console.write(f"Loaded messages: {len(session.messages)}")
            self.console.write(f"配置文件：{self.store.path}")
            self.console.write("输入 /help 查看命令；输入 /exit 退出。")
            return

        self.console.write("=" * 56)
        self.console.write("  Mini Harness Interactive CLI")
        self.console.write("=" * 56)
        self.console.write("输入 /help 查看命令；输入 /exit 退出。")
        self.console.write(f"配置文件：{self.store.path}")

    async def _select_config(self, *, force_menu: bool = False) -> Selection | None:
        """展示已有配置或创建新配置。

        Args:
            force_menu: 为 True 时即使存在默认配置也显示选择菜单，供
                ``/model`` 主动切换使用。
        """
        profiles = self.store.load()
        if not profiles:
            self.console.write("\n尚未保存配置，先创建一个运行配置。")
            return await self._create_config()

        if not force_menu:
            default = self.store.default_profile()
            if default is not None:
                try:
                    selection = await self._config_from_profile(default)
                except ProviderConfigurationError as exc:
                    self.console.write(f"默认配置不可用：{exc}")
                else:
                    self.console.write(
                        f"使用默认配置：{default.name} "
                        f"({default.provider} / {default.model})"
                    )
                    return selection

        default = self.store.default_profile()
        default_name = default.name if default is not None else ""
        while True:
            self.console.write("\n选择配置：")
            for index, profile in enumerate(profiles, start=1):
                marker = " [默认]" if profile.name == default_name else ""
                self.console.write(
                    f"  {index}. {profile.name} "
                    f"({profile.provider} / {profile.model}){marker}"
                )
            new_index = len(profiles) + 1
            self.console.write(f"  {new_index}. 新建配置")
            self.console.write("  q. 退出")

            answer = await self.console.read("请选择 [1]: ")
            answer = answer or "1"
            if answer.lower() in {"q", "quit", "exit"}:
                return None

            try:
                choice = int(answer)
            except ValueError:
                self.console.write("请输入有效序号。")
                continue

            if 1 <= choice <= len(profiles):
                try:
                    selection = await self._config_from_profile(
                        profiles[choice - 1]
                    )
                except ProviderConfigurationError as exc:
                    self.console.write(f"配置不可用：{exc}")
                    continue
                try:
                    self.store.set_default(profiles[choice - 1].name)
                except OSError as exc:
                    self.console.write(f"默认配置保存失败：{exc}")
                return selection
            if choice == new_index:
                return await self._create_config()
            self.console.write("序号超出范围，请重新选择。")

    async def _create_config(self) -> Selection:
        """交互式创建新配置，并默认保存为下次启动使用的配置。"""
        preset = await self._choose_preset()
        model = await self._choose_model(preset)
        base_url = await self._choose_base_url(preset)
        api_key = await self._choose_api_key(preset)

        config = ProviderConfig(
            provider=preset.key,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

        if preset.protocol != "mock":
            save = await self._confirm("保存为默认配置? [Y/n]: ", default=True)
            if save:
                default_name = f"{preset.display_name} / {model}"
                name = await self.console.read(f"配置名称 [{default_name}]: ")
                profile = ProviderProfile.from_config(
                    name or default_name,
                    config,
                )
                try:
                    self.store.upsert(profile, set_default=True)
                except OSError as exc:
                    self.console.write(f"配置保存失败，将继续使用当前配置：{exc}")
                else:
                    saved_hint = (
                        "（含 API Key）" if profile.api_key else ""
                    )
                    self.console.write(
                        f"已保存默认配置：{profile.name}{saved_hint}"
                    )

        return Selection(config=config, preset=preset)

    async def _config_from_profile(self, profile: ProviderProfile) -> Selection:
        """将保存的 profile 恢复为运行配置。"""
        try:
            preset = get_preset(profile.provider)
        except ValueError as exc:
            raise ProviderConfigurationError(str(exc)) from exc

        base_url = profile.base_url
        if not base_url and preset.requires_base_url:
            base_url = await self._choose_base_url(preset)
        elif not base_url:
            base_url = preset.default_base_url

        api_key = profile.api_key
        if not api_key:
            api_key = await self._choose_api_key(preset)
            if api_key:
                # 首次为旧 profile 输入 Key 后补写，下次启动即可直接复用。
                try:
                    self.store.upsert(
                        replace(profile, api_key=api_key),
                        set_default=True,
                    )
                except OSError as exc:
                    self.console.write(f"API Key 保存失败：{exc}")
        config = ProviderConfig(
            provider=profile.provider,
            model=profile.model,
            api_key=api_key,
            base_url=base_url,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
        )
        return Selection(config=config, preset=preset)

    async def _choose_preset(self) -> ProviderPreset:
        """展示分组 Provider 菜单并返回选择。"""
        self.console.write("\n选择 Provider：")
        numbered: list[ProviderPreset] = []
        for group_name, presets in grouped_presets():
            self.console.write(f"\n[{group_name}]")
            for preset in presets:
                numbered.append(preset)
                self.console.write(
                    f"  {len(numbered):2}. {preset.display_name} "
                    f"({preset.key})"
                )

        while True:
            answer = await self.console.read(f"请选择 Provider [1-{len(numbered)}]: ")
            if answer.lower() in {"q", "quit"}:
                raise _AbortSelection
            answer = answer or "1"
            try:
                choice = int(answer)
            except ValueError:
                self.console.write("请输入有效序号。")
                continue
            if 1 <= choice <= len(numbered):
                return numbered[choice - 1]
            self.console.write("序号超出范围，请重新选择。")

    async def _choose_model(self, preset: ProviderPreset) -> str:
        """展示模型建议并允许选择或输入自定义模型 ID。"""
        if not preset.models:
            while True:
                model = await self.console.read("模型 ID（必填）: ")
                if model:
                    return model
                self.console.write("模型 ID 不能为空。")

        self.console.write("\n选择模型：")
        default_index = 1
        if preset.default_model in preset.models:
            default_index = preset.models.index(preset.default_model) + 1
        for index, model in enumerate(preset.models, start=1):
            self.console.write(f"  {index}. {model}")
        self.console.write("  0. 自定义模型 ID")

        while True:
            answer = await self.console.read(f"请选择模型 [{default_index}]: ")
            answer = answer or str(default_index)
            try:
                choice = int(answer)
            except ValueError:
                self.console.write("请输入有效序号。")
                continue
            if choice == 0:
                model = await self.console.read("模型 ID: ")
                if model:
                    return model
                self.console.write("模型 ID 不能为空。")
                continue
            if 1 <= choice <= len(preset.models):
                return preset.models[choice - 1]
            self.console.write("序号超出范围，请重新选择。")

    async def _choose_base_url(self, preset: ProviderPreset) -> str:
        """选择或输入 Base URL。"""
        if preset.default_base_url:
            value = await self.console.read(
                f"Base URL [{preset.default_base_url}]: "
            )
            return value or preset.default_base_url

        if preset.base_url_env:
            env_base_url = os.getenv(preset.base_url_env, "").strip()
            if env_base_url:
                value = await self.console.read(
                    f"Base URL [{env_base_url}]: "
                )
                return value or env_base_url

        if not preset.requires_base_url:
            return ""

        while True:
            value = await self.console.read("Base URL（必填）: ")
            if value:
                return value
            self.console.write("Base URL 不能为空。")

    async def _choose_api_key(self, preset: ProviderPreset) -> str:
        """解析 API Key：环境变量优先，否则在当前会话中输入。"""
        if preset.protocol == "mock":
            return ""
        if not preset.api_key_required and not preset.env_keys:
            return ""

        if preset.env_keys:
            for env_name in preset.env_keys:
                env_value = os.getenv(env_name, "").strip()
                if env_value:
                    self.console.write(
                        f"检测到环境变量 {env_name}，将使用其中的 API Key。"
                    )
                    return ""

        if preset.api_key_required:
            env_label = preset.env_keys[0] if preset.env_keys else "必填"
            while True:
                key = await self.console.read_secret(f"API Key（{env_label}）: ")
                if key:
                    return key
                self.console.write("API Key 不能为空。")

        if preset.key == "custom":
            self.console.write(
                "提示：自定义端点如需鉴权，请填写 API Key；"
                "本地无鉴权服务可直接回车。"
            )
        return await self.console.read_secret("API Key（可留空）: ")

    async def _confirm(self, prompt: str, *, default: bool) -> bool:
        """读取 y/N 确认。"""
        answer = (await self.console.read(prompt)).lower()
        if not answer:
            return default
        return answer in {"y", "yes", "是"}

    async def _chat_loop_with_session(
        self,
        provider: LLMProvider,
        selection: Selection,
    ) -> bool:
        """V1：每个用户回合通过 MessageBus + AgentLoop 执行并持久化。"""
        self.console.write("")
        self.console.write(
            f"已连接：{selection.preset.display_name} / {selection.config.model}"
        )
        self.console.write(
            "命令：/model 切换厂商或模型  /history 查看历史  "
            "/clear 清空 Session  /tools 查看工具  /help 帮助  /exit 退出"
        )

        while True:
            try:
                user_text = await self.console.read(self.console.user_prompt())
            except EOFError:
                self.console.write("")
                return False

            if not user_text:
                continue

            command = user_text.strip().lower()
            if command in {"/exit", "/quit", "exit", "quit"}:
                return False
            if command == "/help":
                self.console.write(
                    "/model 或 /config 切换 Provider/模型（Session 历史保留）；"
                    "/history 查看当前 Session；/clear 清空当前 Session；"
                    "/tools 查看已注册工具；/exit 退出。"
                )
                continue
            if command == "/history":
                self._print_history()
                continue
            if command == "/clear":
                try:
                    self.session_manager.clear()  # type: ignore[union-attr]
                except SessionError as exc:
                    self.console.write_error(f"清空 Session 失败：{exc}")
                else:
                    self.console.write("已清空当前 Session 历史。")
                continue
            if command in {"/config", "/model"}:
                self.console.write(
                    "选择新的 Provider / 模型；当前 Session 历史保留。"
                )
                try:
                    new_selection = await self._select_config(force_menu=True)
                except _AbortSelection:
                    self.console.write("已取消切换。")
                    continue
                if new_selection is None:
                    self.console.write("已取消切换。")
                    continue
                try:
                    new_provider = create_provider(new_selection.config)
                except ProviderConfigurationError as exc:
                    self.console.write(f"配置错误：{exc}")
                    continue
                provider = new_provider
                selection = new_selection
                self.console.write(
                    f"已切换：{selection.preset.display_name} / "
                    f"{selection.config.model}"
                )
                continue
            if command == "/tools":
                self.console.write("已注册工具：" + ", ".join(self.tools.tool_names))
                continue
            if user_text.startswith("/"):
                self.console.write("未知命令，输入 /help 查看可用命令。")
                continue

            renderer = (
                StreamingAssistantRenderer(self.console)
                if self.stream
                else None
            )
            try:
                outbound = await self._run_session_turn(
                    provider,
                    selection,
                    user_text,
                    on_text_delta=renderer.on_delta if renderer else None,
                    on_tool_start=renderer.on_tool_start if renderer else None,
                )
            except Exception as exc:  # noqa: BLE001 - CLI 兜底展示
                if renderer:
                    renderer.finish()
                self.console.write_error(
                    f"请求失败：{type(exc).__name__}: {exc}"
                )
                continue

            if renderer:
                renderer.finish()
            if outbound.metadata.get("error"):
                self.console.write_error(outbound.content)
                continue
            answer = outbound.content
            if not answer.strip():
                answer = "（模型返回了空内容）"
            tools_used = outbound.metadata.get("tools_used", [])
            # 流式模式下工具提示已经在执行前逐条打印过，避免重复。
            if tools_used and not (renderer and renderer.tools_printed):
                self.console.write_tool(", ".join(tools_used))
            # 流式回调已经打印过最终回答时，不能再次 write_assistant。
            if not outbound.metadata.get("streamed"):
                self.console.write_assistant(answer)

    async def _run_session_turn(
        self,
        provider: LLMProvider,
        selection: Selection,
        user_text: str,
        on_text_delta: TextDeltaCallback | None = None,
        on_tool_start: Callable[[str], None] | None = None,
    ) -> OutboundMessage:
        """通过一次独立的 AgentLoop 任务处理一个用户回合。

        每次用户输入都构造 MessageBus + AgentLoop，把 Session 的加载、
        Runner 调用和保存全部留在 AgentLoop 内，CLI 只负责投递输入和读取
        出站结果。这样也避免 CLI 直接修改 Session 文件。

        Args:
            on_text_delta: 可选流式回调；由 AgentLoop 透传到 Provider。
        """
        turn_provider = provider
        if selection.preset.protocol == "mock":
            # MockProvider 的响应序列是一次性的；每个用户回合使用新的
            # MockProvider，模拟真实 Provider 可跨回合复用的行为。
            turn_provider = create_provider(selection.config)

        bus = MessageBus()
        runner = AgentRunner(provider=turn_provider, tools=self.tools)
        agent_loop = AgentLoop(
            bus=bus,
            runner=runner,
            max_iterations=self.max_iterations,
            poll_interval=0.01,
            session_manager=self.session_manager,
            on_text_delta=on_text_delta,
        )
        loop_task = asyncio.create_task(agent_loop.run())
        try:
            await bus.publish_inbound(
                InboundMessage(content=user_text, sender="cli")
            )
            try:
                return await asyncio.wait_for(
                    bus.consume_outbound(),
                    timeout=120.0,
                )
            except TimeoutError:
                return OutboundMessage(
                    content="Agent 响应超时。",
                    metadata={"error": "timeout", "tools_used": []},
                )
        finally:
            agent_loop.stop()
            await loop_task

    def _print_history(self) -> None:
        """打印当前 Session 历史；命令本身不会发送给 LLM。"""
        try:
            session = self.session_manager.load()  # type: ignore[union-attr]
        except SessionError as exc:
            self.console.write_error(f"Session 加载失败：{exc}")
            return
        if not session.messages:
            self.console.write("当前 Session 历史为空。")
            return
        self.console.write(f"当前 Session 历史（{len(session.messages)} 条）：")
        for index, message in enumerate(session.messages, start=1):
            role = str(message.get("role", "unknown"))
            content = message.get("content")
            if content is None:
                content = ""
            line = f"  {index}. {role}: {content}"
            tool_calls = message.get("tool_calls")
            if tool_calls:
                line += f" [tool_calls={len(tool_calls)}]"
            if message.get("tool_call_id"):
                line += f" [tool_call_id={message['tool_call_id']}]"
            self.console.write(line)

    async def _chat_loop(
        self,
        provider: LLMProvider,
        selection: Selection,
    ) -> bool:
        """运行多轮聊天；返回 False 表示退出。"""
        if self.session_manager is not None:
            return await self._chat_loop_with_session(provider, selection)
        runner = AgentRunner(provider=provider, tools=self.tools)
        messages: list[Message] = []

        self.console.write("")
        self.console.write(
            f"已连接：{selection.preset.display_name} / {selection.config.model}"
        )
        self.console.write(
            "命令：/model 切换厂商或模型  /clear 清空上下文  "
            "/tools 查看工具  /help 帮助  /exit 退出"
        )

        while True:
            try:
                user_text = await self.console.read(self.console.user_prompt())
            except EOFError:
                self.console.write("")
                return False

            if not user_text:
                continue

            command = user_text.lower()
            if command in {"/exit", "/quit", "exit", "quit"}:
                return False
            if command == "/help":
                self.console.write(
                    "/model 或 /config 切换 Provider/模型（保留当前上下文）；"
                    "/clear 清空消息历史；/tools 查看已注册工具；/exit 退出。"
                )
                continue
            if command == "/clear":
                messages.clear()
                self.console.write("已清空当前会话上下文。")
                continue
            if command in {"/config", "/model"}:
                self.console.write(
                    "选择新的 Provider / 模型；当前会话上下文保留。"
                )
                try:
                    new_selection = await self._select_config(force_menu=True)
                except _AbortSelection:
                    self.console.write("已取消切换。")
                    continue
                if new_selection is None:
                    self.console.write("已取消切换。")
                    continue
                try:
                    new_provider = create_provider(new_selection.config)
                except ProviderConfigurationError as exc:
                    self.console.write(f"配置错误：{exc}")
                    continue
                provider = new_provider
                selection = new_selection
                runner = AgentRunner(provider=provider, tools=self.tools)
                self.console.write(
                    f"已切换：{selection.preset.display_name} / "
                    f"{selection.config.model}"
                )
                continue
            if command == "/tools":
                self.console.write("已注册工具：" + ", ".join(self.tools.tool_names))
                continue
            if user_text.startswith("/"):
                self.console.write("未知命令，输入 /help 查看可用命令。")
                continue

            turn_messages = [*messages, {"role": "user", "content": user_text}]
            renderer = (
                StreamingAssistantRenderer(self.console)
                if self.stream
                else None
            )
            try:
                # MockProvider 的响应序列是有限的（一次工具调用 + 一次最终
                # 回答），每个用户回合都必须重新创建，否则第二个用户回合
                # 会耗尽响应序列。真实 Provider 可以跨回合复用同一个实例。
                turn_runner = runner
                if selection.preset.protocol == "mock":
                    turn_runner = AgentRunner(
                        provider=create_provider(selection.config),
                        tools=self.tools,
                    )
                result = await turn_runner.run(
                    AgentRunSpec(
                        initial_messages=turn_messages,
                        max_iterations=self.max_iterations,
                        on_text_delta=renderer.on_delta if renderer else None,
                    )
                )
            except ProviderError as exc:
                if renderer:
                    renderer.finish()
                self.console.write_error(f"Provider 错误：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - CLI 需要兜底展示错误
                if renderer:
                    renderer.finish()
                self.console.write_error(
                    f"请求失败：{type(exc).__name__}: {exc}"
                )
                continue

            if renderer:
                renderer.finish()
            answer = result.final_content
            if answer is None:
                answer = "（已达到最大迭代次数，未生成最终回答）"
            elif not answer.strip():
                # 某些供应商会返回空 content（例如只返回 reasoning 内容），
                # 明确提示而不是打印一行只有角色标签的空消息。
                answer = "（模型返回了空内容）"
            if result.tools_used:
                self.console.write_tool(", ".join(result.tools_used))
            if not result.streamed_final:
                self.console.write_assistant(answer)
            messages = result.messages


async def run_interactive_cli(
    workspace: str | Path | None = None,
    data_root: Path | None = None,
    max_iterations: int = 32,
    stream: bool = True,
) -> None:
    """启动 V1 交互式 CLI。

    Args:
        workspace: 显式 ``--workspace``；为 None 时使用调用进程的 cwd。
        data_root: Session 数据目录；默认 ``~/.mini-harness``。
        max_iterations: 单次 Agent 运行的最大迭代次数。
        stream: 是否启用流式输出，默认开启；``--no-stream`` 可关闭。
    """
    await InteractiveCLI(
        workspace=workspace if workspace is not None else Path.cwd(),
        data_root=data_root,
        max_iterations=max_iterations,
        stream=stream,
    ).run()
