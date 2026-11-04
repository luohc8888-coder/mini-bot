# Mini Harness 项目总结

> 本文基于 README、`pyproject.toml`、`main.py` 以及核心源码（`agent/loop.py`、`agent/runner.py`、`workspace.py`、`providers/base.py`、`cli/main.py`、`tests/test_streaming.py`）整理。
>
> ⚠️ **注意**：README 中把"流式响应"列为 V1 不支持项，但当前代码实际已实现流式输出（见第四节与第六节）。本总结以源码为准。

## 一、项目简介

**Mini Harness** 是一个基于 Python `asyncio` 构建的最小 **Agent Harness**，目的是从零理解真实 Agent Harness 的分层设计。它参考了 [HKUDS/nanobot](https://github.com/HKUDS/nanobot) 的分层思路，但有意保持轻量：不依赖 Agent 框架、不使用数据库，只保留消息总线、Agent Loop、Agent Runner、Provider、ToolRegistry，以及 V1 的 Session/Workspace 产品层。

- **版本**：`0.2.0`（Mini Harness V1）
- **Python 要求**：`>=3.11`
- **核心依赖**：`httpx[socks]`（唯一运行时依赖）
- **开发依赖**：`pytest`、`pytest-asyncio`（`asyncio_mode = auto`）
- **安装入口**：`mini-bot` 与 `mini-harness` 两个等价命令

## 二、整体架构

核心数据流（正常聊天路径）：

```text
CLI → InboundMessage → MessageBus → AgentLoop → AgentRunner
    → LLMProvider / ToolRegistry → ToolResult → AgentRunner
    → AgentRunResult → OutboundMessage → MessageBus → CLI
```

流式模式下，Provider 通过 `on_text_delta` 回调把文本增量实时回传给 CLI 逐段渲染，最终回答不会再被重复打印（靠 `streamed_final` 标记）。

V1 完整启动路径：

```text
WorkspaceResolver（固定 Workspace 绝对路径、计算稳定 workspace_id）
    → SessionManager（加载 Session: cli:main，有历史则恢复 messages）
    → CLI 读取输入 → InboundMessage → MessageBus → AgentLoop
    → Session.messages + 当前用户消息 → AgentRunner: LLM → Tool → LLM
    → AgentRunResult → 校验完整历史 → 更新 Session.messages
    → SessionManager.save()（原子写入）→ OutboundMessage → MessageBus → CLI
```

`--demo` 模式仍走 V0 的 MessageBus + AgentLoop 路径，只是不启用 Session。

## 三、模块职责

| 模块 | 职责 |
|---|---|
| `workspace.py` | 解析并固定 Workspace，计算稳定 ID，提供路径边界检查 |
| `session/manager.py` | 定义 `Session`，负责 JSON 读取、原子保存和清空 |
| `agent/loop.py` | 消费 `InboundMessage`，加载/更新/保存 Session，再发布 `OutboundMessage` |
| `agent/prompt.py` | 按 Workspace 构造最小 System Prompt |
| `agent/runner.py` | 只执行模型/工具迭代，不感知 Session 文件或 Workspace；支持流式回调 |
| `bus/` | 消息总线，隔离输入输出通道与 Agent 核心 |
| `cli/app.py` / `cli/main.py` / `cli/profiles.py` | 配置选择、CLI 命令、流式终端渲染、Provider Profile |
| `providers/` | Provider 抽象、工厂、预设、OpenAI-compatible / Anthropic / Mock 适配器，含 `chat_stream` |
| `tools/` | 工具注册、参数校验和执行；包含 `AddTool`、`GrepTool`、`ListDirTool`、`ReadFileTool`、`WriteFileTool` |

## 四、核心组件说明（源码确认）

### 1. AgentLoop（`agent/loop.py`）
- 单消费者异步调度层：从 `MessageBus` 消费入站消息，调用 `AgentRunner`，再发布 `OutboundMessage`。
- **不直接调用 Provider，也不执行工具**，只负责输入适配、Session 历史读写、调用 Runner。
- 使用短超时轮询（`poll_interval`）+ `_stop_requested` 标志实现可正常退出的常驻循环。
- `session_manager` 为可选参数：传入时启用 V1 Session 历史加载/更新/持久化；不传时保持 V0 单轮无状态行为。
- 只有 `stop_reason` 属于 `{"completed", "max_iterations_finalized"}` 的回合才写入 Session；并**严格校验 Runner 返回的消息是否是输入消息的完整前缀**，否则拒绝保存。
- 出站 metadata 中带 `"streamed": bool(result.streamed_final)`，供 CLI 判断是否已流式展示过。

### 2. AgentRunner（`agent/runner.py`）
- 真正执行"模型调用 → 工具调用 → 再调用模型"的核心循环。
- `AgentRunSpec` 输入字段：`initial_messages`、`max_iterations`、`system_prompt`、`finalize_on_max_iterations`、**`on_text_delta`（流式文本回调）**、**`on_tool_start`（工具开始回调）**。
- `AgentRunResult` 输出字段：`final_content`、`messages`、`tools_used`、`stop_reason`、**`streamed_final`**（最终回答是否已通过流式输出过，避免重复打印）。
- 深拷贝 `initial_messages`，不修改调用方列表。
- 每轮调用 Provider 时**临时拼接 `system` 消息**，不写入 `result.messages` 或 Session。
- 工具调用时先追加 assistant 的 `tool_calls`，再逐个执行工具并追加 `role=tool` 结果（靠 `tool_call_id` 配对）；执行前可触发 `on_tool_start`。
- **流式请求逻辑封装在 `_request()`**：无回调时走普通 `chat`；有回调且 Provider 实现 `chat_stream` 则走流式；否则退化为普通 `chat` + 一次性回调（兼容只实现 `chat` 的第三方 Provider）。
- 预算耗尽时 `finalize_on_max_iterations` 会再请求一次**不带工具**的总结，停止原因记为 `max_iterations_finalized`。

### 3. Provider 抽象（`providers/base.py`）
- 类型别名：`Message`、`ToolDefinition`，以及流式相关回调 **`TextDeltaCallback = Callable[[str], None]`**、**`ToolStartCallback = Callable[[str], None]`**。
- `LLMProvider` 抽象接口：抽象方法 `async chat(messages, tools)`；并提供**默认实现的 `async chat_stream(messages, tools, on_text_delta)`**——默认退化为一次普通 `chat` 后一次性回调，支持 SSE 的 Provider 覆盖它以真正边收边回调。
- `LLMResponse` 提供 `has_tool_calls` / `should_execute_tools` 属性。
- 协议支持：OpenAI-compatible、Anthropic Messages、离线 Mock，三者均已实现 `chat_stream`（SSE 分片聚合文本与 tool_calls）。
- 内置 **23 个 Provider 预设**，覆盖国内外厂商、第三方聚合平台、本地 Ollama / LM Studio 和自定义端点。

### 4. Workspace（`workspace.py`）
- `workspace_id(path)`：`sha256(规范化绝对路径)[:16]`，同路径跨进程稳定、不同路径隔离、不使用随机 UUID。
- `Workspace`（frozen dataclass）：持有 `path` 和 `id`，`resolve_path()` 处理 `../`、绝对路径、符号链接越界。
- `WorkspaceResolver`：解析 `--workspace` 或默认 `Path.cwd()`，启动时固定，不随后续 CWD 改变而切换。
- **安全说明**：`Path.resolve()` + `relative_to()` 不是完整安全沙箱，符号链接竞态是后续强化项。

## 五、V1 关键特性

- **Session 持久化**：单 Channel、单默认 Session（`DEFAULT_SESSION_KEY = "cli:main"`），JSON 格式带 `schema_version`，UTF-8、`ensure_ascii=False`，保存 assistant 的 `tool_calls` 和 tool 的 `tool_call_id`。
- **存储位置**：`~/.mini-harness/`（可用 `MINI_HARNESS_HOME` 覆盖），Session 历史**不写入 Workspace**。结构为 `sessions/<workspace-id>/session.json`。
- **原子保存**：同目录临时文件 → 写入 + flush + fsync → chmod 0600 → `os.replace()` → 尽量 fsync 父目录。
- **单实例锁**：CLI 在 macOS/Linux 对 `session.lock` 加非阻塞 `flock`，第二个实例报 `SessionInUseError`（Windows 退化为不锁）。
- **文件工具**：`list_dir` / `grep` / `read_file` / `write_file`，均通过 `Workspace.resolve_path()` 做路径校验，只支持 UTF-8 文本/Markdown。
- **System Prompt**：按 Workspace 临时拼接，指导模型先用 `list_dir`、优先读关键文件、用 `grep` 定位、读 3~8 个文件后写报告。
- **运行预算**：`max_iterations` 默认 32，可用 `--max-iterations` 调整，耗尽时收尾。
- **流式输出（已实现）**：CLI 默认开启流式，Provider 逐段回调文本增量、CLI 实时渲染；`--no-stream` 可关闭。Runner 只用完整 assistant 消息写入历史，中间 chunk 不污染 `messages`。
- **输出长度**：`max_tokens` 默认 4096，旧配置的 1024 会自动迁移。

## 六、交互式 CLI

启动后本地处理以下命令（不发送给 LLM）：

| 命令 | 作用 |
|---|---|
| `/help` | 查看命令帮助 |
| `/model` / `/config` | 切换 Provider / 模型（保留历史） |
| `/history` | 查看当前 Session 历史 |
| `/clear` | 清空当前 Session（内存 + 磁盘） |
| `/tools` | 查看已注册工具 |
| `/exit` / `/quit` | 安全退出 |

主要命令行参数：

| 参数 | 作用 |
|---|---|
| `--interactive` | 进入交互式 CLI |
| `--workspace PATH` | 指定 Workspace（默认 CWD） |
| `--max-iterations N` | 单次运行最大迭代轮数（默认 32） |
| `--stream` / `--no-stream` | 开启/关闭流式输出（默认开启） |
| `--demo` | 离线工具调用演示 |
| `--list-providers` | 列出 Provider 预设 |

配置保存在 `~/.mini-harness/config.json`（权限 0600），API Key 只存于此、不写入 Session。设置 `MINI_HARNESS_NO_COLOR=1` 可关闭颜色。

## 七、项目结构

```text
mini-harness/
├── main.py                    # 兼容入口：交互式 / --demo / --list-providers
├── pyproject.toml
├── uv.lock
├── README.md
├── s.md
├── mini_harness/
│   ├── __init__.py / __main__.py
│   ├── workspace.py           # Workspace / WorkspaceResolver / workspace_id
│   ├── bus/                   # events.py, queue.py
│   ├── agent/                 # loop.py, prompt.py, runner.py
│   ├── session/               # manager.py, workspace.py
│   ├── cli/                   # app.py, main.py, profiles.py
│   ├── providers/             # base/config/presets/factory/openai_compat/anthropic/mock
│   └── tools/                 # base.py, filesystem.py, registry.py
├── mini_harness.egg-info/     # 打包元数据（构建产物）
└── tests/                     # 11 个测试文件
```

## 八、测试

```bash
uv run pytest -q
```

测试文件（`tests/` 共 11 个）：

| 测试文件 | 覆盖内容 |
|---|---|
| `test_bus.py` | 消息总线往返、asyncio.Queue FIFO |
| `test_prompt.py` | System Prompt 的 Workspace 路径与关键规则 |
| `test_runner.py` | MockProvider、消息历史、tool_call_id 配对、max_iterations、收尾、参数校验、工具异常 |
| `test_e2e.py` | V0 完整 E2E 调用链、CLI 演示输出、Loop 启停时序 |
| `test_providers.py` | 预设完整性、工厂、环境变量、OpenAI/Anthropic 转换、HTTP 错误 |
| `test_cli.py` | Profile 存储与权限、API Key、默认配置、max_tokens 迁移、`/model` 切换 |
| `test_workspace.py` | 稳定 workspace_id、路径隔离、`../`、绝对路径与符号链接越界 |
| `test_session.py` | 缺失/损坏 JSON、Workspace 冲突、清空、原子保存、单实例锁 |
| `test_session_integration.py` | 重启恢复、Workspace 隔离、历史不重复、Provider 入参、失败回合不落盘 |
| `test_filesystem_tools.py` | `list_dir`/`grep`/`read_file`/`write_file` 的发现、过滤、校验、分页、UTF-8 |
| `test_streaming.py` | Provider 流式分片聚合（Mock/OpenAI/Anthropic）、Runner 回调不写中间消息、CLI 流式不重复打印 |

> README 记录的测试结果为 `97 passed`（该数字基于含 `test_streaming.py` 之前/之后的版本可能已变化，具体以实际运行 `uv run pytest -q` 为准）。自动化测试全部使用 `httpx` MockTransport 或 `MockProvider`，不发起外网请求。

## 九、已知限制与后续方向

**当前限制**：
- Workspace 重命名/移动会产生新 `workspace_id`，不自动迁移旧 Session。
- 只有一个 CLI Channel、一个默认 Session，不支持多 Session 切换。
- JSON 持久化是单进程单写者方案，`flock` 只保护 CLI。
- 不做 ContextBuilder、上下文裁剪/压缩；无 Memory、Hooks、Checkpoint、MCP、多通道。
- 文件工具只支持 UTF-8 文本，不支持图片/PDF/Office/`edit_file`/shell。
- 路径检查不是完整安全沙箱。

**后续建议（V2）**：ContextBuilder（Token 预算）、Session Queue / 并发控制、SQLite SessionStore、更完整的文件工具、Memory/Skills/Hooks/Checkpoint/MCP。

## 十、快速开始

```bash
cd mini-harness
uv sync
uv tool install --editable .        # 安装全局命令

mini-harness --interactive          # 交互式 CLI（默认流式）
mini-harness --interactive --workspace /path/to/project
mini-harness --interactive --workspace /path/to/project --max-iterations 50
mini-harness --interactive --no-stream   # 关闭流式
mini-harness --demo                 # 完全离线的工具调用演示

# 兼容入口
uv run python main.py --interactive
python -m mini_harness --interactive
```

不带参数时：终端环境进入交互式 CLI；非交互环境（管道/CI）自动运行 `--demo`。
