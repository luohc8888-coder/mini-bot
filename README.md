# Mini Harness

Mini Harness 是一个基于 Python `asyncio` 构建的最小 Agent Harness，用于学习和演示 LLM Agent 的完整工具调用闭环：

```text
接收消息 → 调用模型 → 执行工具 → 再次调用模型 → 返回结果
```

项目参考 [Nanobot](https://github.com/HKUDS/nanobot) 的分层设计，但有意保持轻量：不依赖 Agent 框架、不使用数据库，只保留消息总线、Agent Loop、Agent Runner、Provider、ToolRegistry 等核心概念。

当前版本在 V0 工具调用闭环之上，增加了**自定义 Provider 工厂**和**交互式多轮 CLI**：既可以使用离线 MockProvider 学习完整链路，也可以选择常见大模型厂商、第三方聚合平台或任意 OpenAI-compatible 服务进行真实对话。

## 当前状态

- 已实现可运行、可测试的 Agent 工具调用闭环。
- 已实现 Provider 工厂、厂商预设、OpenAI-compatible 与 Anthropic 两种 HTTP 协议适配器。
- 已实现交互式 CLI：选择 Provider、模型、Base URL、API Key，保存命名配置，并在会话中切换配置。
- 内置 23 个 Provider 预设，覆盖国内外厂商、第三方聚合平台、本地 Ollama/LM Studio 和自定义 OpenAI-compatible 服务。
- 内置离线 `MockProvider`，无需网络和 API Key 即可体验完整工具调用流程。
- 真实厂商 API 需要自行提供 API Key；自动化测试全部使用 httpx MockTransport，不会发起外网请求。

## 主要特性

- **异步消息总线**：基于 `asyncio.Queue` 解耦 CLI 与 Agent 核心，入站/出站消息独立排队。
- **清晰的 Agent 分层**：`AgentLoop` 负责消息调度，`AgentRunner` 负责模型与工具迭代，职责互不混杂。
- **可扩展的 Provider 工厂**：通过 `ProviderPreset` 描述厂商，`create_provider()` 统一构建 Provider，新增厂商只需增加预设或适配器。
- **两种协议适配**：OpenAI Chat Completions-compatible 覆盖多数厂商；Anthropic Messages API 单独适配 `tool_use` / `tool_result` 内容块。
- **交互式多轮 CLI**：菜单选择配置、保存 Profile、默认配置自动启动、运行时 `/model` 切换 Provider/模型、`/clear` 清空上下文。
- **清晰的终端输出**：用户、助手、工具和错误使用不同颜色；模型返回空内容时显示明确提示。
- **安全的工具调用**：工具注册、JSON Schema 参数校验、异常兜底和 `tool_call_id` 配对。
- **完整消息历史**：记录 user、assistant tool calls、tool results 和最终 assistant 回答。
- **可控的迭代上限**：`max_iterations` 防止工具调用无限循环。
- **最小依赖**：运行时依赖 `httpx[socks]`（支持 HTTP/SOCKS 代理），测试使用 pytest。

## 架构

项目有两个使用入口，共用同一套 AgentRunner、Provider 和 ToolRegistry：

```text
# 离线演示：完整 V0 消息链路
CLI (main.py --demo)
 ⇅  InboundMessage / OutboundMessage
MessageBus
 ⇅
AgentLoop
 ⇅
AgentRunner  ⇄  LLMProvider（MockProvider）
 ⇅
ToolRegistry → AddTool

# 交互式 CLI：直接驱动 Runner，并在 CLI 侧维护多轮历史
InteractiveCLI
 ⇅
AgentRunner  ⇄  LLMProvider（OpenAI-compatible / Anthropic / Mock）
 ⇅
ToolRegistry → AddTool
```

### 一次工具调用的执行流程

以 `--demo` 为例：

1. CLI 创建 `InboundMessage` 并发布到 `MessageBus`。
2. `AgentLoop` 消费入站消息，构造 `AgentRunSpec` 并交给 `AgentRunner`。
3. `AgentRunner` 调用 `MockProvider.chat(messages, tools)`。
4. MockProvider 返回 `add(a=2, b=3)` 的工具调用请求。
5. `AgentRunner` 追加 assistant tool call 消息，调用 `ToolRegistry.execute()`，得到 `ToolResult("5")`。
6. Runner 追加带 `tool_call_id` 的 tool 结果消息，并再次调用 MockProvider。
7. MockProvider 根据 tool 结果返回 `"结果是 5"`，Runner 生成 `AgentRunResult`。
8. `AgentLoop` 包装成 `OutboundMessage` 发回 MessageBus，CLI 读取并打印。

交互式 CLI 的每一轮对话直接调用 `AgentRunner.run()`，并把上一轮返回的 `result.messages` 作为下一轮历史，因此支持连续多轮对话和工具调用。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

已使用 Python 3.14.5 和 uv 0.11.24 验证运行。

## 快速开始

```bash
cd mini-harness
uv sync

# 安装全局命令（从任意目录启动）
uv tool install --editable .

# 进入交互式 CLI（推荐）
mini-bot --interactive

# 或运行完全离线的工具调用演示
mini-bot --demo

# 查看所有内置 Provider 预设
mini-bot --list-providers
```

也可以继续使用项目内的兼容入口：

```bash
uv run python main.py --interactive
uv run python main.py --demo
uv run python main.py --list-providers
```

安装后会同时提供 `mini-bot` 和 `mini-harness` 两个命令；`mini-bot` 是推荐名称。

`mini-bot` 不带参数时：在终端中会进入交互式 CLI；在非交互环境（例如管道、CI）中会自动运行 `--demo`，避免等待输入。

`--demo` 预期输出：

```text
结果是 5
```

首次执行 `uv sync` 会创建虚拟环境并安装 `httpx`（含 SOCKS 代理支持）与测试依赖。

如果终端设置了 `ALL_PROXY=socks5://...` 之类的代理环境变量，`httpx` 会通过代理访问模型 API；项目已包含 `httpx[socks]`，正常执行 `uv sync` 即可避免 `socksio` 缺失错误。

## 交互式 CLI 使用

首次启动、或者没有默认配置时会显示 Provider 分组菜单：

```text
[快速体验]
  1. Mock（离线演示）
[国内厂商]
  2. DeepSeek
  3. 阿里云百炼 / Qwen
  ...
```

选择 Provider 后，再选择模型（也可以输入自定义模型 ID）、确认 Base URL，并输入 API Key。API Key 优先从环境变量读取；如果没有设置，CLI 会提示输入且不回显。创建配置时默认会保存为下次启动使用的默认配置；本次输入的 API Key 也会写入权限为 0600 的本地配置文件，便于直接复用。之后启动会直接使用默认配置，不再显示选择菜单；运行中可用 `/model` 切换配置。

### 环境变量

常用厂商的 API Key 环境变量：

| Provider | 环境变量 |
|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` |
| 阿里云百炼 / Qwen | `DASHSCOPE_API_KEY` |
| 智谱 GLM | `ZHIPUAI_API_KEY`（也兼容 `ZAI_API_KEY`） |
| Moonshot / Kimi | `MOONSHOT_API_KEY` |
| Kimi Coding | `KIMI_CODING_API_KEY` |
| 魔搭 ModelScope | `MODELSCOPE_API_KEY` |
| MiniMax | `MINIMAX_API_KEY` |
| Huawei Cloud MaaS | `HUAWEI_MAAS_API_KEY` |
| Huawei MaaS Direct | `HUAWEI_MAAS_DIRECT_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Google Gemini | `GEMINI_API_KEY` |
| xAI Grok | `XAI_API_KEY` |
| Mistral | `MISTRAL_API_KEY` |
| Groq | `GROQ_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| SiliconFlow | `SILICONFLOW_API_KEY` |
| AIHubMix | `AIHUBMIX_API_KEY` |
| Novita AI | `NOVITA_API_KEY` |
| Ollama | 无需 API Key |
| LM Studio | 无需 API Key |
| 自定义 OpenAI-compatible | `MINI_HARNESS_API_KEY`（可留空，若服务端要求鉴权则必须填写） |

示例：

```bash
export DEEPSEEK_API_KEY="sk-..."
mini-bot --interactive
# 选择 [国内厂商] → DeepSeek → deepseek-chat
```

华为云 MaaS 的 Base URL 以控制台提供的 OpenAI-compatible 地址为准，预设中不硬编码；CLI 会要求输入 Base URL，也可以预先设置 `HUAWEI_MAAS_BASE_URL` 或 `HUAWEI_MAAS_DIRECT_BASE_URL`。模型列表中已包含截图中的 `DeepSeek-v4.1-Flash`、`GLM-5.3`、`Qwen-300` 和 `deepseek-v4.1-flash-1017`。自定义 OpenAI-compatible 也可以用 `MINI_HARNESS_BASE_URL` 提供 Base URL。

如果自定义端点返回 `Failed to get the authorization header` 或 HTTP 401/403，说明服务端要求鉴权：请填写 API Key，或设置 `MINI_HARNESS_API_KEY`。本地无鉴权端点可以留空；需要非 Bearer 的额外 Header 时，可通过代码中的 `ProviderConfig(extra_headers={...})` 配置。

### 会话内命令

| 命令 | 作用 |
|---|---|
| `/help` | 查看命令帮助 |
| `/model` | 切换 Provider / 模型（保留当前对话上下文） |
| `/config` | `/model` 的别名，切换配置 |
| `/clear` | 清空当前会话消息历史 |
| `/tools` | 查看已注册工具 |
| `/exit` 或 `/quit` | 退出 CLI |

交互式终端中用户、助手、工具和错误会使用不同颜色；设置 `MINI_HARNESS_NO_COLOR=1` 可关闭颜色。

### 保存配置

创建新配置时默认保存为“默认配置”，下次启动会自动使用该配置，不再每次显示选择菜单；在运行中可用 `/model` 切换其他已保存配置。配置文件默认位于 `~/.mini-harness/config.json`，保存 Provider、模型、Base URL、温度、max tokens 以及本次输入的 API Key，文件权限为 `0600`。如果 API Key 来自环境变量，则不会重复写入文件。

安全提示：API Key 以明文保存在本机配置文件中，请确保该文件权限为 `0600`，不要提交到版本库或共享给他人。可通过以下环境变量覆盖路径：

- `MINI_HARNESS_HOME`：配置目录，默认 `~/.mini-harness`
- `MINI_HARNESS_CONFIG`：直接指定配置文件路径

## Provider 预设

| Key | 厂商 / 平台 | 协议 | 默认 Base URL | 默认模型 |
|---|---|---|---|---|
| `mock` | Mock 离线演示 | Mock | 无需 | `mock-add-demo` |
| `deepseek` | DeepSeek | OpenAI-compatible | `https://api.deepseek.com/v1` | `deepseek-chat` |
| `qwen` | 阿里云百炼 / Qwen | OpenAI-compatible | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| `zhipu` | 智谱 GLM | OpenAI-compatible | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| `moonshot` | Moonshot / Kimi | OpenAI-compatible | `https://api.moonshot.ai/v1` | `moonshot-v1-8k` |
| `kimi-coding` | Kimi Coding | Anthropic Messages | `https://api.kimi.com/coding/v1` | `kimi-k2.5` |
| `modelscope` | 魔搭 ModelScope | OpenAI-compatible | `https://api-inference.modelscope.cn/v1` | `Qwen/Qwen3-235B-A22B` |
| `minimax` | MiniMax | OpenAI-compatible | `https://api.minimax.io/v1` | `MiniMax-M3.1-Flash` |
| `huawei-maas` | Huawei Cloud MaaS | OpenAI-compatible | 控制台提供，CLI 输入 | `deepseek-v4.1-flash-1017` |
| `huawei-maas-direct` | Huawei MaaS Direct | OpenAI-compatible | 控制台提供，CLI 输入 | `deepseek-v4.1-flash-1017` |
| `openai` | OpenAI | OpenAI-compatible | `https://api.openai.com/v1` | `gpt-4.1-mini` |
| `anthropic` | Anthropic Claude | Anthropic Messages | `https://api.anthropic.com/v1` | `claude-sonnet-4-5` |
| `gemini` | Google Gemini | OpenAI-compatible | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-flash` |
| `xai` | xAI Grok | OpenAI-compatible | `https://api.x.ai/v1` | `grok-3-mini` |
| `mistral` | Mistral AI | OpenAI-compatible | `https://api.mistral.ai/v1` | `mistral-large-latest` |
| `groq` | Groq | OpenAI-compatible | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| `openrouter` | OpenRouter | OpenAI-compatible | `https://openrouter.ai/api/v1` | `openai/gpt-4.1-mini` |
| `siliconflow` | 硅基流动 SiliconFlow | OpenAI-compatible | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` |
| `aihubmix` | AIHubMix | OpenAI-compatible | `https://aihubmix.com/v1` | `gpt-4.1-mini` |
| `novita` | Novita AI | OpenAI-compatible | `https://api.novita.ai/openai` | `deepseek/deepseek-chat` |
| `ollama` | Ollama 本地 | OpenAI-compatible | `http://localhost:11434/v1` | `llama3.2` |
| `lm-studio` | LM Studio 本地 | OpenAI-compatible | `http://localhost:1234/v1` | `local-model` |
| `custom` | 自定义 OpenAI-compatible | OpenAI-compatible | CLI 输入 | CLI 输入 |

模型列表是建议值，模型 ID 会随厂商更新；在 CLI 中选择“自定义模型 ID”即可输入任意模型名。

### 代码中使用 ProviderFactory

除交互式 CLI 外，也可以直接在代码中使用工厂：

```python
from mini_harness.providers import ProviderConfig, ProviderFactory

provider = ProviderFactory().create(
    ProviderConfig(provider="deepseek", model="deepseek-chat")
)
```

API Key 默认从 `DEEPSEEK_API_KEY` 读取，也可以显式传入 `api_key`。

## 项目结构

```text
mini-harness/
├── main.py                       # 兼容入口：交互式 / --demo / --list-providers
├── pyproject.toml                # 依赖、入口命令与 pytest 配置
├── uv.lock
├── README.md
├── mini_harness/
│   ├── __init__.py
│   ├── __main__.py               # 支持 python -m mini_harness
│   ├── bus/
│   │   ├── events.py             # InboundMessage / OutboundMessage
│   │   └── queue.py              # MessageBus
│   ├── agent/
│   │   ├── loop.py               # AgentLoop
│   │   └── runner.py             # AgentRunner / AgentRunSpec / AgentRunResult
│   ├── cli/
│   │   ├── app.py                # 交互式菜单与多轮聊天
│   │   ├── main.py               # 可安装的 mini-bot CLI 入口
│   │   └── profiles.py           # 默认配置、API Key 与 Profile 持久化
│   ├── providers/
│   │   ├── base.py               # Provider 抽象与错误类型
│   │   ├── config.py             # ProviderConfig
│   │   ├── presets.py            # 23 个厂商预设与分组
│   │   ├── factory.py            # Provider 工厂
│   │   ├── openai_compat.py      # OpenAI-compatible HTTP 适配器
│   │   ├── anthropic.py          # Anthropic Messages API 适配器
│   │   └── mock.py               # MockProvider
│   └── tools/
│       ├── base.py               # Tool / ToolResult / 参数校验
│       └── registry.py           # ToolRegistry + AddTool
└── tests/
    ├── test_bus.py
    ├── test_runner.py
    ├── test_e2e.py
    ├── test_providers.py         # 预设、工厂、OpenAI/Anthropic 转换
    └── test_cli.py               # 配置存储与交互式菜单
```

## 核心模块

| 模块 | 说明 |
|---|---|
| `bus/events.py` | 定义入站和出站消息数据结构 |
| `bus/queue.py` | 使用两个 `asyncio.Queue` 实现异步消息总线 |
| `agent/loop.py` | 单消费者循环，负责消费消息、调用 Runner、发布结果 |
| `agent/runner.py` | 核心 Agent 循环，维护消息历史并执行工具调用 |
| `providers/base.py` | `LLMProvider`、`LLMResponse`、`ToolCallRequest`、`ProviderError` |
| `providers/presets.py` | 厂商元数据、默认模型、Base URL、API Key 环境变量 |
| `providers/factory.py` | 读取配置、解析 Key/Base URL、构建 Provider |
| `providers/openai_compat.py` | OpenAI Chat Completions-compatible HTTP 实现 |
| `providers/anthropic.py` | Anthropic Messages API 实现 |
| `providers/mock.py` | 按预设序列返回响应，支持根据工具结果生成最终回答 |
| `cli/app.py` | Provider/模型选择、多轮聊天、运行中切换配置 |
| `cli/profiles.py` | `~/.mini-harness/config.json` 配置存储 |
| `tools/base.py` | 工具抽象、工具结果和参数 Schema 校验 |
| `tools/registry.py` | 工具注册、查找、执行与异常处理，内置 `AddTool` |
| `main.py` | 兼容入口，转发到 `mini_harness.cli.main` |
| `cli/main.py` | 可安装的 `mini-bot` 命令行入口 |

### 消息约定

- `assistant.tool_calls[].id` 与 `tool.tool_call_id` 一一对应。
- 每个工具调用都会产生一条 `role=tool` 消息，即使工具执行失败也是如此。
- 工具失败会作为错误消息回传给模型，不会直接中断 Agent。
- OpenAI-compatible 适配器在发送时把 `arguments` 转成 JSON 字符串；接收时再解析回 `dict`。
- Anthropic 适配器把工具调用转成 `tool_use`，把工具结果转成 `tool_result` 内容块。
- 达到 `max_iterations` 时返回 `stop_reason="max_iterations"`。

## 测试

```bash
uv run pytest -q
```

预期结果：

```text
50 passed
```

测试覆盖：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_bus.py` | 入站/出站消息往返、asyncio.Queue FIFO |
| `tests/test_runner.py` | MockProvider、消息历史、tool_call_id 配对、max_iterations、参数校验、工具异常 |
| `tests/test_e2e.py` | 完整 E2E 调用链、CLI 演示输出、Loop 正常退出与停止时序 |
| `tests/test_providers.py` | 预设完整性、工厂构建、环境变量、OpenAI/Anthropic 请求响应转换、HTTP 错误 |
| `tests/test_cli.py` | Profile 存储与权限、API Key 持久化、默认配置、Mock 多轮对话、`/model` 切换、Provider 目录 |

## 待做功能

已完成：

- [x] **自定义 Provider 工厂**：常见厂商、第三方平台、本地 Ollama/LM Studio 与自定义 OpenAI-compatible。
- [x] **真实 LLM Provider**：OpenAI-compatible 与 Anthropic Messages API。
- [x] **交互式多轮 CLI**：配置选择、默认配置自动启动、持久化 Profile 与 API Key、运行时 `/model` 切换 Provider/模型。

后续可继续实现：

- [ ] **Session 管理**：会话隔离、历史记录、Session Queue 与 Session Lock。
- [ ] **Context Engineering**：ContextBuilder、Token 预算、上下文裁剪与压缩。
- [ ] **Memory Engineering**：长期记忆、检索、写入与 Consolidation。
- [ ] **Hooks**：Agent 回合与工具执行的生命周期钩子。
- [ ] **Checkpoint**：运行状态保存、中断恢复和任务重放。
- [ ] **MCP**：接入 MCP Server，发现并调用外部工具。
- [ ] **流式响应**：支持 token 流式输出。
- [ ] **网络健壮性**：重试、指数退避、代理和更细粒度超时。
- [ ] **多模态输入**：图片、音频、文件等消息内容。
- [ ] **并发与运行中注入**：多消费者、Session 并发控制、运行中追加用户消息。
- [ ] **多通道支持**：Web、Slack、Telegram 等消息通道适配器。
- [ ] **更多内置工具**：文件读写、Shell、搜索等常用工具。

## 参考

- [HKUDS/nanobot](https://github.com/HKUDS/nanobot)
- [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat)
- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages)
- Python `asyncio` 文档：https://docs.python.org/3/library/asyncio.html
