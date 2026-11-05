"""Mini Harness 交互式 CLI。

优先复用默认配置启动；没有默认配置时选择 Provider / 模型 / API Key。支持
保存默认配置、通过 ``/model`` 切换 Provider/模型、``/settings`` 配置推理强度，以及
Workspace 固定、Session 历史恢复、``/history`` 和 ``/clear``。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mini_harness.agent import (
    AgentLoop,
    AgentRunner,
    ContextBuilder,
    ContextBudgetExceeded,
    ContextConfig,
)
from mini_harness.bus import InboundMessage, MessageBus, OutboundMessage
from mini_harness.providers import LLMProvider, ProviderConfigurationError, create_provider
from mini_harness.providers.base import TextDeltaCallback, ToolStartCallback
from mini_harness.providers.config import validate_max_output_tokens
from mini_harness.session import SessionError, SessionManager
from mini_harness.tools import (
    AddTool,
    ApplyPatchTool,
    EditFileTool,
    FileInfoTool,
    FindFilesTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    RunCommandTool,
    ToolRegistry,
    WriteFileTool,
)

from mini_harness.workspace import WorkspaceResolver

from .console import Console, StreamingAssistantRenderer
from .settings import Selection, _AbortSelection, ProviderSettingsMixin
from .runtime import MemoryMaintenanceMixin

from .profiles import ProfileStore, ProfileStoreError


class InteractiveCLI(ProviderSettingsMixin, MemoryMaintenanceMixin):
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
        context_config: ContextConfig | None = None,
        turn_timeout: float | None = None,
        max_output_tokens: int | None = None,
        context_window_tokens: int = 0,
        thinking: bool = False,
        reasoning_effort: str = "",
        max_context_tokens: int = 0,
        reserve_output_tokens: int = 0,
        memory_store: object | None = None,
        session_compactor: object | None = None,
        dream_manager: object | None = None,
        maintenance_enabled: bool = True,
        idle_compact_after_minutes: float | None = None,
        dream_interval_hours: float | None = None,
        dream_enabled: bool | None = None,
    ) -> None:
        """初始化 CLI 依赖。

        Args:
            workspace: Workspace 路径；省略时使用当前工作目录。
            data_root: Session 数据根目录，便于测试或自定义 Harness 状态位置。
            session_manager: 可注入的已有 SessionManager，优先于 workspace。
            stream: 是否启用流式输出；``run_interactive_cli`` 默认开启，
                直接实例化时默认关闭以保持旧行为。
            context_config: V2 上下文预算配置；默认 ``ContextConfig()``。
            turn_timeout: 可选整轮等待秒数；None 表示不设固定整轮超时，
                避免长生成已经成功保存却被 UI 误报 timeout。
            memory_store: 可注入的 MemoryStore，便于测试；默认按 Workspace
                创建 ``<workspace>/memory``。
            session_compactor: 可注入的持久化压缩器；默认按当前 Provider
                延迟创建。
            dream_manager: 可注入的 Dream 管理器；默认按当前 Provider 延迟创建。
            maintenance_enabled: 是否启动 Idle Compaction / Dream 后台任务；
                测试可关闭以保持确定性。
            idle_compact_after_minutes: 覆盖空闲压缩阈值；0 关闭。
            dream_interval_hours: 覆盖自动 Dream 检查间隔小时数。
            dream_enabled: 覆盖自动 Dream 开关；手动 ``/dream`` 不受影响。
        """
        self.console = console or Console()
        self.store = profile_store or ProfileStore()
        self.max_iterations = max_iterations
        self.stream = stream
        self.context_config = context_config or ContextConfig()
        self._base_context_config = self.context_config
        self._context_config_override = context_config
        self._max_context_tokens_override = max_context_tokens or (
            context_config.max_context_tokens if context_config else 0
        )
        self._reserve_output_tokens_override = reserve_output_tokens or (
            context_config.reserve_output_tokens if context_config else 0
        )
        self.turn_timeout = turn_timeout
        if max_output_tokens is not None:
            validate_max_output_tokens(max_output_tokens)
        self.max_output_tokens = max_output_tokens
        self.context_window_tokens = context_window_tokens
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.tools = ToolRegistry()
        self.tools.register(AddTool())

        if session_manager is not None:
            self.session_manager = session_manager
            self.workspace = session_manager.workspace
        else:
            resolved = WorkspaceResolver().resolve(workspace)
            self.session_manager = SessionManager(resolved, data_root=data_root)
            self.workspace = resolved.path

        # V2.2：长期记忆与持久化压缩运行态。Provider 在真正连接后绑定。
        self.memory_store = memory_store
        if self.memory_store is None:
            try:
                from mini_harness.memory.store import MemoryStore

                self.memory_store = MemoryStore(self.workspace)
            except Exception:  # noqa: BLE001 - 记忆不可用不能阻止 CLI 启动
                self.memory_store = None
        self._session_compactor = session_compactor
        self._dream_manager = dream_manager
        self._maintenance_enabled = maintenance_enabled
        self._idle_compact_after_minutes = idle_compact_after_minutes
        self._dream_interval_hours = dream_interval_hours
        self._dream_enabled = dream_enabled
        self._maintenance_tasks: list[asyncio.Task] = []
        self._idle_scheduler = None
        self._dream_scheduler = None
        self._active_session_key: str | None = None
        self._active_provider: LLMProvider | None = None

        # 文件工具必须绑定固定的 Workspace，不能读取进程 cwd。
        file_workspace = WorkspaceResolver().resolve(self.workspace)
        self.tools.register(FindFilesTool(file_workspace))
        self.tools.register(GrepTool(file_workspace))
        self.tools.register(ListDirTool(file_workspace))
        self.tools.register(ReadFileTool(file_workspace))
        self.tools.register(WriteFileTool(file_workspace))
        self.tools.register(EditFileTool(file_workspace))
        self.tools.register(ApplyPatchTool(file_workspace))
        self.tools.register(FileInfoTool(file_workspace))
        self.tools.register(RunCommandTool(file_workspace, confirm=self._confirm_command))

        # V2：CLI 与 AgentLoop 共享同一个 ContextBuilder；Tool Schema 开销
        # 按当前注册表估算，文件工具必须已在上方注册完成。
        self.context_builder = ContextBuilder(
            self.workspace,
            self.context_config,
            tool_definitions=self.tools.get_definitions(),
            memory_store=self.memory_store,
        )

    async def _confirm_command(self, command: str, cwd: str) -> bool:
        """宿主侧确认，批准值不来自模型提供的工具参数。"""
        self.console.write(f"命令需要确认（cwd: {cwd}）：\n{command}")
        try:
            answer = await self.console.read("允许执行？输入 yes 确认，其他输入拒绝：")
        except EOFError:
            return False
        return answer.strip().lower() == "yes"


    async def run(self) -> None:
        """运行配置选择与聊天主循环。"""
        lock_context = self.session_manager.lock()
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

                        self._ensure_memory_runtime(provider)
                        self._start_maintenance()
                        try:
                            should_switch = await self._chat_loop(
                                provider,
                                selection,
                            )
                        finally:
                            await self._stop_maintenance()
                        if not should_switch:
                            self.console.write("已退出。")
                            return
                except (KeyboardInterrupt, EOFError):
                    self.console.write("\n已退出。")
        except (SessionError, ProfileStoreError) as exc:
            self.console.write_error(str(exc))

    def _show_banner(self) -> None:
        """显示当前 Workspace / Session / 上下文预算。"""
        self.console.write("")
        session = self.session_manager.load()
        self.console.write("Mini Harness V2.2")
        self.console.write(f"Workspace: {self.workspace}")
        self.console.write(f"Session: {self.session_manager.session_key}")
        self.console.write(f"History: {len(session.messages)} messages")
        self.console.write(f"Loaded messages: {len(session.messages)}")
        if session.summary:
            self.console.write(
                f"Summary: 已归档 {session.summary_boundary} 条历史"
            )
        self.console.write(
            f"Context budget: {self.context_config.available_input_tokens} "
            f"input tokens (max {self.context_config.max_context_tokens}, "
            f"reserve {self.context_config.reserve_output_tokens})"
        )
        self.console.write(f"配置文件：{self.store.path}")
        self.console.write(
            "输入 /help 查看命令；输入 /context 查看上下文预算；"
            "输入 /compact 压缩历史；输入 /dream 整理长期记忆；"
            "输入 /exit 退出。"
        )

    async def _chat_loop(
        self,
        provider: LLMProvider,
        selection: Selection,
    ) -> bool:
        """每个用户回合通过同一条 Session / MessageBus 路径执行。"""
        self._activate_selection(selection)
        self.console.write("")
        self.console.write(
            f"已连接：{selection.preset.display_name} / {selection.config.model}"
        )
        self.console.write(
            "命令：/model 切换厂商或模型  /settings 推理设置  /history 查看历史  "
            "/context 查看上下文  /compact 压缩历史  /dream 整理长期记忆  "
            "/last-prompt 查看上次请求  /clear 清空 Session  /tools 查看工具  "
            "/help 帮助  /exit 退出"
        )
        self._ensure_memory_runtime(provider)

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
                    "/settings 设置并保存当前模型的推理强度；"
                    "/history 查看当前 Session；/context 查看模型上下文与预算；"
                    "/compact 立即压缩当前 Session 的未归档历史；"
                    "/dream 立即整理长期记忆（与自动 Dream 互斥）；"
                    "/last-prompt 查看上一次发给模型的完整请求消息；"
                    "/clear 清空当前 Session（不清空长期记忆）；/tools 查看已注册工具；"
                    "/exit 退出。"
                )
                continue
            if command == "/compact":
                await self._handle_compact_command()
                continue
            if command == "/dream":
                await self._handle_dream_command()
                continue
            if command == "/settings":
                updated = await self._edit_reasoning_settings(selection)
                if updated is not None:
                    provider, selection = updated
                    self._bind_memory_provider(provider)
                continue
            if command == "/history":
                self._print_history()
                continue
            if command == "/context":
                self._print_context()
                continue
            if command in {"/last-prompt", "/last"}:
                self._print_last_prompt()
                continue
            if command == "/clear":
                try:
                    self.session_manager.clear()  # type: ignore[union-attr]
                except SessionError as exc:
                    self.console.write_error(f"清空 Session 失败：{exc}")
                else:
                    self.context_builder.last_provider_request = None
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
                self._activate_selection(selection)
                self._bind_memory_provider(provider)
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
        on_tool_start: ToolStartCallback | None = None,
    ) -> OutboundMessage:
        """通过一次独立的 AgentLoop 任务处理一个用户回合。

        每次用户输入都构造 MessageBus + AgentLoop，把 Session 的加载、
        Runner 调用和保存全部留在 AgentLoop 内，CLI 只负责投递输入和读取
        出站结果。这样也避免 CLI 直接修改 Session 文件。

        Args:
            on_text_delta: 可选流式回调；由 AgentLoop 透传到 Provider。
            on_tool_start: 可选工具开始回调；由 AgentLoop 透传到 Runner。
        """
        turn_provider = provider
        if selection.preset.protocol == "mock":
            # MockProvider 的响应序列是一次性的；每个用户回合使用新的
            # MockProvider，模拟真实 Provider 可跨回合复用的行为。
            turn_provider = create_provider(selection.config)

        resolver = getattr(self, "_effective_context_config", None)
        effective_context = (
            resolver(selection) if callable(resolver) else self.context_config
        )
        self.context_config = effective_context
        if self._session_compactor is not None and getattr(
            self._session_compactor, "context_config", None
        ) is not None:
            self._session_compactor.context_config = effective_context
        builder = getattr(self, "context_builder", None)
        if builder is not None:
            builder.config = effective_context
        bus = MessageBus()
        runner = AgentRunner(provider=turn_provider, tools=self.tools)
        agent_loop = AgentLoop(
            bus=bus,
            runner=runner,
            max_iterations=self.max_iterations,
            poll_interval=0.01,
            session_manager=self.session_manager,
            context_builder=self.context_builder,
            context_config=self.context_config,
            memory_store=self.memory_store,
            on_text_delta=on_text_delta,
            on_tool_start=on_tool_start,
        )
        session_key = (
            self.session_manager.session_key
            if self.session_manager is not None
            else None
        )
        if self._idle_scheduler is not None and session_key is not None:
            self._idle_scheduler.mark_active(session_key)
            # 若空闲压缩已持有检查锁（可能正在生成摘要），等它完成后再读
            # Session，避免新回合与压缩互相覆盖。
            try:
                await self._idle_scheduler.wait_for_check_completion()
            except Exception:  # noqa: BLE001 - 调度器异常不能阻止用户回合
                pass
        loop_task = asyncio.create_task(agent_loop.run())
        try:
            await bus.publish_inbound(
                InboundMessage(content=user_text, sender="cli")
            )
            turn_timeout = getattr(self, "turn_timeout", None)
            if turn_timeout is not None and turn_timeout > 0:
                try:
                    return await asyncio.wait_for(
                        bus.consume_outbound(),
                        timeout=turn_timeout,
                    )
                except TimeoutError:
                    return OutboundMessage(
                        content="Agent 响应超时。",
                        metadata={"error": "timeout", "tools_used": []},
                    )
            # 默认不设固定整轮总时限：持续正常的流式生成/工具执行不应
            # 在 120 秒被误报为失败。需要总时限的调用方显式传 turn_timeout。
            return await bus.consume_outbound()
        finally:
            if self._idle_scheduler is not None and session_key is not None:
                self._idle_scheduler.mark_inactive(session_key)
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
            if session.summary:
                self.console.write(
                    f"当前 Summary 已覆盖 {session.summary_boundary} 条历史。"
                )
            return
        self.console.write(f"当前 Session 历史（{len(session.messages)} 条）：")
        if session.summary:
            self.console.write(
                f"  [Summary] 已归档前 {session.summary_boundary} 条历史，"
                f"摘要约 {len(session.summary)} 字符；原始历史仍完整保留。"
            )
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

    def _print_context(self) -> None:
        """显示 ContextBuilder 的真实计算结果。

        该命令只读取 Session 并执行纯内存构建，不会保存 Session、不会触发
        LLM 调用，也不会打印完整聊天正文或 API Key。统计中的
        ``估算输入 Token`` 不含当前尚未输入的下一句用户消息。
        """
        try:
            session = self.session_manager.load()
        except SessionError as exc:
            self.console.write_error(f"Session 加载失败：{exc}")
            return
        try:
            stats = self.context_builder.inspect(
                session.messages,
                summary=session.summary,
                summary_boundary=session.summary_boundary,
            )
        except (ContextBudgetExceeded, ValueError) as exc:
            self.console.write_error(f"上下文统计失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 调试命令不应让 CLI 崩溃
            self.console.write_error(
                f"上下文统计失败：{type(exc).__name__}: {exc}"
            )
            return

        self.console.write("")
        self.console.write("上下文信息")
        self.console.write(f"工作目录：{stats.workspace}")
        self.console.write(
            "系统提示词：已加载"
            f"（{stats.system_prompt_tokens} 估算 Token）"
        )
        self.console.write(
            f"AGENTS.md：{self._format_agents_md_status(stats)}"
        )
        self.console.write(
            f"工具定义：{stats.tool_schema_tokens} 估算 Token"
        )
        self.console.write(
            f"长期记忆：{stats.long_term_memory_status}"
            f"（{stats.long_term_memory_tokens} 估算 Token）"
        )
        self.console.write(
            f"Session Summary：{stats.summary_tokens} 估算 Token"
            f"（已归档边界 {stats.summary_boundary} 条）"
        )
        self.console.write(
            f"未归档历史：{stats.unarchived_message_count} 条"
        )
        self.console.write(
            f"安全余量：{stats.safety_margin_tokens} 估算 Token"
        )
        self.console.write(f"会话消息数：{stats.session_message_count}")
        self.console.write(
            "会话角色："
            f"{self._format_role_counts(stats.session_role_counts)}"
        )
        self.console.write(
            "已选历史消息数："
            f"{stats.selected_history_messages}"
        )
        self.console.write(
            "已丢弃历史消息数："
            f"{stats.dropped_history_messages}"
        )
        if stats.history_digest_count:
            self.console.write(
                f"完成轮次摘要数：{stats.history_digest_count}"
            )
        self.console.write(
            "已选历史角色："
            f"{self._format_role_counts(stats.selected_history_role_counts)}"
        )
        self.console.write(
            f"估算输入 Token：{stats.estimated_input_tokens}"
        )
        self.console.write(
            f"配置输入预算：{stats.available_input_tokens}"
        )
        self.console.write(f"历史预算：{stats.history_budget_tokens}")
        self.console.write(
            f"最大上下文 Token：{stats.max_context_tokens}"
            f"（输出预留：{stats.reserve_output_tokens}）"
        )

    def _print_last_prompt(self) -> None:
        """打印当前进程最近一次真正发给 Provider 的请求。

        数据来自 AgentRunner 在 ``provider.chat`` 调用边界记录的快照，
        因此能反映工具循环中的最后一次请求（iteration、Compact 状态、
        实际工具消息数），而不是 ContextBuilder 初次构建的版本。快照只
        保存在内存中，不包含 API Key 或认证 Header。
        """
        snapshot = self.context_builder.last_provider_request
        messages = snapshot.messages if snapshot is not None else None
        if not messages:
            self.console.write("当前进程还没有发送过模型请求。")
            return

        self.console.write("")
        if snapshot is not None:
            self.console.write(
                "最近一次 Provider 请求："
                f"iteration={snapshot.iteration}，"
                f"第 {snapshot.call_index} 次请求，"
                f"估算输入 {snapshot.estimated_tokens}/"
                f"{snapshot.available_input_tokens} tokens，"
                f"类型={snapshot.request_kind}，"
                f"输出上限 {snapshot.max_output_tokens} tokens，"
                f"accepted 边界 {snapshot.accepted_message_count} 条，"
                f"Compact={'是' if snapshot.compacted else '否'}，"
                f"accepted Compact="
                f"{'是' if snapshot.accepted_compacted else '否'}，"
                f"工具消息 {snapshot.tool_message_count} 条"
            )
            if snapshot.compaction_error:
                self.console.write(
                    f"Compact 回退原因：{snapshot.compaction_error}"
                )
        self.console.write(
            f"上一次发给模型的请求消息（{len(messages)} 条）："
        )
        for index, message in enumerate(messages, start=1):
            role = str(message.get("role", "unknown"))
            self.console.write(f"\n[{index}] {role}")
            content = message.get("content")
            if content is None:
                self.console.write("（无内容）")
            elif content == "":
                self.console.write("（空内容）")
            else:
                self.console.write(str(content))

            tool_calls = message.get("tool_calls")
            if tool_calls:
                self.console.write(
                    "tool_calls: "
                    + json.dumps(tool_calls, ensure_ascii=False, indent=2)
                )
            if message.get("tool_call_id"):
                self.console.write(
                    f"tool_call_id: {message['tool_call_id']}"
                )
            if message.get("name"):
                self.console.write(f"name: {message['name']}")

        tool_definitions = snapshot.tool_definitions if snapshot is not None else None
        self.console.write("")
        if tool_definitions:
            self.console.write(
                "工具定义（不属于 messages，作为 Provider 的 tools 参数"
                f"单独发送，共 {len(tool_definitions)} 个）："
            )
            self.console.write(
                json.dumps(tool_definitions, ensure_ascii=False, indent=2)
            )
        else:
            self.console.write("工具定义：无（当前请求没有注册工具）。")

    @staticmethod
    def _format_agents_md_status(stats: object) -> str:
        """把 ContextStats 的 AGENTS.md 状态转成 CLI 可读中文文案。"""
        status = str(getattr(stats, "agents_md_status", "missing"))
        chars = int(getattr(stats, "agents_md_chars", 0))
        tokens = int(getattr(stats, "agents_md_tokens", 0))
        if status == "loaded":
            return f"已加载（{chars} 字符，{tokens} 估算 Token）"
        labels = {
            "missing": "未找到",
            "empty": "空文件（已忽略）",
            "disabled": "已禁用",
            "too_large": "已跳过（超过大小限制）",
            "decode_error": "已跳过（非 UTF-8）",
            "read_error": "已跳过（读取失败）",
            "not_file": "未找到",
        }
        return labels.get(status, status)

    @staticmethod
    def _format_role_counts(counts: dict[str, int]) -> str:
        """把角色统计格式化为 ``user=1, assistant=2``。"""
        if not counts:
            return "（无）"
        return ", ".join(
            f"{role}={count}" for role, count in sorted(counts.items())
        )


async def run_interactive_cli(
    workspace: str | Path | None = None,
    data_root: Path | None = None,
    max_iterations: int = 32,
    stream: bool = True,
    max_context_tokens: int = 0,
    reserve_output_tokens: int = 0,
    max_output_tokens: int | None = None,
    context_window_tokens: int = 0,
    thinking: bool = False,
    reasoning_effort: str = "",
    idle_compact_after_minutes: float | None = None,
    dream_interval_hours: float | None = None,
    no_dream: bool = False,
) -> None:
    """启动 V2 交互式 CLI。

    Args:
        workspace: 显式 ``--workspace``；为 None 时使用调用进程的 cwd。
        data_root: Session 数据目录；默认 ``~/.mini-harness``。
        max_iterations: 单次 Agent 运行的最大迭代次数。
        stream: 是否启用流式输出，默认开启；``--no-stream`` 可关闭。
        max_context_tokens: 显式上下文窗口覆盖；0 表示使用 profile/默认值。
        reserve_output_tokens: 显式输出预留覆盖；0 表示使用模型配置。
        max_output_tokens: 本次覆盖模型最大输出额度，新建 profile 时保存。
        context_window_tokens: 本次覆盖模型上下文窗口，新建 profile 时保存。
        thinking: 本次显式开启 thinking，新建 profile 时保存。
        reasoning_effort: 本次覆盖推理档位，新建 profile 时保存。
        idle_compact_after_minutes: 覆盖空闲压缩阈值；0 关闭。
        dream_interval_hours: 覆盖自动 Dream 间隔小时数。
        no_dream: 关闭自动 Dream（手动 /dream 不受影响）。
    """
    await InteractiveCLI(
        workspace=workspace if workspace is not None else Path.cwd(),
        data_root=data_root,
        max_iterations=max_iterations,
        stream=stream,
        max_context_tokens=max_context_tokens,
        reserve_output_tokens=reserve_output_tokens,
        max_output_tokens=max_output_tokens,
        context_window_tokens=context_window_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        idle_compact_after_minutes=idle_compact_after_minutes,
        dream_interval_hours=dream_interval_hours,
        dream_enabled=False if no_dream else None,
    ).run()
