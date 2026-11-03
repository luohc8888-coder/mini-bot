# Mini Harness

Mini Harness 是一个基于 Python `asyncio` 构建的最小 Agent Harness，用于从零理解真实 Agent Harness 的分层设计：

```text
CLI → InboundMessage → MessageBus → AgentLoop → AgentRunner
    → LLMProvider / ToolRegistry → ToolResult → AgentRunner
    → AgentRunResult → OutboundMessage → MessageBus → CLI
```

项目参考 [HKUDS/nanobot](https://github.com/HKUDS/nanobot) 的分层思路，但有意保持轻量：不依赖 Agent 框架、不使用数据库，只保留消息总线、Agent Loop、Agent Runner、Provider、ToolRegistry 和 V1 的 Session/Workspace 产品层。

当前版本 **Mini Harness V1** 在 V0 的工具调用闭环和真实 Provider 支持之上，增加了：

- Workspace 解析与稳定的 `workspace_id`
- 单 Channel、单默认 Session 的 JSON 持久化
- 多轮对话跨进程重启恢复
- `/history`、`/clear`、`/exit` CLI 命令
- 原子保存与基本的单实例 CLI 锁
- 基于 Workspace 路径策略的 `list_dir` / `grep` / `read_file` / `write_file` 文本工具
- 最小 System Prompt、32 轮默认预算与预算耗尽收尾

## 当前状态

- 可运行、可测试的 Agent 工具调用闭环。
- Provider 工厂、厂商预设、OpenAI-compatible 与 Anthropic 两种 HTTP 协议适配器。
- 内置 23 个 Provider 预设，覆盖国内外厂商、第三方聚合平台、本地 Ollama/LM Studio 和自定义 OpenAI-compatible 服务。
- 内置离线 `MockProvider`，无需网络和 API Key 即可体验完整工具调用流程。
- V1 Session 历史保存在 Harness 数据目录，不写入用户 Workspace。
- 真实厂商 API 需要自行提供 API Key；自动化测试全部使用 httpx MockTransport 或 MockProvider，不会发起外网请求。

## 快速开始

```bash
cd mini-harness
uv sync

# 从任意目录安装并启动全局命令
uv tool install --editable .

# 进入交互式 CLI（推荐）
mini-harness --interactive

# 显式指定 Workspace
mini-harness --interactive --workspace /path/to/project

# 分析较大项目时提高单次 Agent 的最大迭代次数
mini-harness --interactive --workspace /path/to/project --max-iterations 50

# 或运行完全离线的工具调用演示
mini-harness --demo
```

`mini-bot` 是等价的安装命令。安装后会同时提供 `mini-bot` 和 `mini-harness` 两个入口。

也可以继续使用项目内兼容入口：

```bash
uv run python main.py --interactive
uv run python main.py --demo
uv run python main.py --list-providers
python -m mini_harness --interactive
```

`mini-harness` / `mini-bot` 不带参数时：终端环境进入交互式 CLI；非交互环境（例如管道、CI）自动运行 `--demo`，避免等待输入。

## V1 架构

### 正常聊天路径

```text
启动 Mini Harness
    ↓
WorkspaceResolver
    ↓
固定 Workspace 绝对路径，计算稳定 workspace_id
    ↓
SessionManager
    ↓
加载 Session(cli:main)
    │   有历史 → 恢复 messages
    │   无历史 → 创建空 Session
    ↓
CLI 读取用户输入
    ↓
InboundMessage
    ↓
MessageBus
    ↓
AgentLoop
    ↓
Session.messages + 当前用户消息
    ↓
AgentRunner: LLM → Tool → LLM
    ↓
AgentRunResult
    ↓
校验 Runner 返回的完整历史 → 更新 Session.messages
    ↓
SessionManager.save()（原子写入）
    ↓
OutboundMessage
    ↓
MessageBus
    ↓
CLI 显示回答
```

`--demo` 仍然走 V0 的 MessageBus + AgentLoop 路径，只是不启用 Session。

### 模块职责

| 模块 | 职责 |
|---|---|
| `workspace.py` | 解析并固定 Workspace，计算稳定 ID，提供路径边界检查 |
| `session/manager.py` | 定义 `Session`，负责 JSON 读取、原子保存和清空 |
| `agent/loop.py` | 消费 `InboundMessage`，加载/更新/保存 Session，再发布 `OutboundMessage` |
| `agent/prompt.py` | 按 Workspace 构造最小 System Prompt |
| `agent/runner.py` | 只执行模型/工具迭代，不感知 Session 文件或 Workspace |
| `bus/` | 隔离输入输出通道与 Agent 核心 |
| `cli/app.py` | 配置选择、CLI 命令和终端展示 |
| `tools/` | 工具注册、参数校验和执行；包含 `AddTool`、`GrepTool`、`ListDirTool`、`ReadFileTool`、`WriteFileTool` |

Session 持久化代码没有放入 `AgentRunner` 或 `ToolRegistry`，以保持 V0 的职责划分。

## Workspace

### 解析规则

默认使用当前进程工作目录：

```python
workspace = Path.cwd().resolve()
```

支持显式指定：

```bash
mini-harness --workspace /path/to/project
```

优先级：

1. `--workspace` 显式传入的路径
2. `Path.cwd()`

Workspace 在启动时固定为 `Workspace` 对象，不会因为后续工具改变进程工作目录而改变；文件工具应使用 `Workspace.path` 作为根，而不是再次读取 `Path.cwd()`。

启动示例：

```text
Mini Harness V1
Workspace: /private/tmp
Session: cli:main
History: 4 messages
Loaded messages: 4
```

### Workspace ID

V1 使用规范化绝对路径计算稳定 ID：

```python
workspace_id = hashlib.sha256(
    str(workspace.resolve()).encode("utf-8")
).hexdigest()[:16]
```

- 同一路径在不同进程启动后得到相同 ID。
- 不同路径得到不同 ID。
- 不使用随机 UUID。
- Session 文件同时记录完整 Workspace 路径，加载时校验冲突。

**V1 已知限制：** Workspace 被重命名或移动后，绝对路径变化会产生新的 ID，旧 Session 不会自动迁移。Nanobot 的持久 workspace-id 标记、路径迁移和复制检测不在 V1 实现范围。

### Workspace 文件路径策略

`list_dir`、`grep`、`read_file` 和 `write_file` 都通过 `Workspace.resolve_path()` 解析路径，并处理：

- `../` 路径穿越
- Workspace 外的绝对路径
- 已存在且指向 Workspace 外部的符号链接

示例：

```python
workspace = WorkspaceResolver().resolve()
path = workspace.resolve_path("README.md")  # /workspace/README.md
```

文件工具只支持 UTF-8 文本/Markdown：`read_file` 返回带行号的内容，`write_file` 创建父目录并覆盖目标文件。它们不会改变进程当前工作目录，也不会读取 Workspace 外的文件。

**安全说明：** `Path.resolve()` + `relative_to()` 不是完整的安全沙箱。路径检查与实际打开文件之间仍可能存在并发替换符号链接的竞态，V1 将这一点明确列为后续安全强化项，而不是宣称提供了完整文件系统隔离。

### 文件工具

V1 在 Workspace 内提供四个最小文本文件工具，参考 Nanobot 的 `list_dir` / `read_file` / `write_file` 语义，并补充 `grep`：

```text
list_dir(path, recursive=False, max_entries=200)
grep(pattern, path=".", glob="*", max_matches=100)
read_file(path, offset=1, limit=2000)
write_file(path, content)
```

`list_dir`：

- 列出 Workspace 目录内容，适合让模型先发现项目文件
- 支持 `recursive=true` 递归列出
- 自动忽略 `.git`、`.venv`、`__pycache__`、`node_modules` 等噪声目录
- 默认最多返回 200 项

`grep`：

- 在 Workspace 的 UTF-8 文本文件中搜索正则
- 返回 `相对路径:行号: 命中行`
- 支持 `glob` 过滤，例如 `*.py`
- 默认最多返回 100 条命中

`read_file`：

- 返回带 1-based 行号的内容
- 支持 `offset` / `limit` 分页
- 适合读取 UTF-8 Markdown 文本
- 文件不存在、二进制/非 UTF-8、路径越界都会返回 `ToolResult.error`

`write_file`：

- 创建或完整覆盖目标文件
- 自动创建 Workspace 内的父目录
- 使用同目录临时文件 + `os.replace` 原子写入
- 适合创建新的 Markdown 文件或整体重写文件

文件工具只在 CLI 有 Workspace 时注册；V0 无 Workspace 的兼容调用仍只有 `AddTool`。文件工具需要真实 Provider 具备 tool calling 能力；内置 `MockProvider` 是预设脚本，默认 `mock-add-demo` 仍只会调用 `add`。

### System Prompt 与运行预算

每轮 V1 对话在调用 Provider 前，会用 `build_system_prompt(workspace)` 临时拼接一条 `system` 消息，指导模型：

1. 先用 `list_dir` 看目录结构，需要深层结构时才递归；
2. 优先读取 README、`pyproject.toml`、入口文件和核心模块，不要逐个读取所有源码；
3. 定位类、函数或关键字时优先使用 `grep`；
4. 通常阅读 3~8 个关键文件后立即写报告；
5. 只依据已读内容写报告，不编造未读文件。

System Prompt 只存在于传给 Provider 的临时消息列表中，不会写入 `result.messages` 或 Session，避免下一轮重复注入。

运行预算：

- `AgentRunSpec.max_iterations` 默认 32，`InteractiveCLI` / `run_interactive_cli` 默认也是 32；
- CLI 支持 `--max-iterations 50` 手动调整；
- 预算耗尽时 `finalize_on_max_iterations` 会再请求一次不带工具的总结，停止原因记为 `max_iterations_finalized`；
- 仍无法收尾的 `max_iterations` 回合不会写入 Session。

### 输出长度

`ProviderConfig.max_tokens` 和 `ProviderProfile.max_tokens` 默认从 1024 提高到 4096，给 `write_file` 的 Markdown 报告留足输出空间。旧 `config.json` 中显式保存的 1024 会在加载时迁移为 4096。

## Session

### 数据结构

```python
@dataclass(slots=True)
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
```

V1 固定默认 Session Key：

```python
DEFAULT_SESSION_KEY = "cli:main"
```

不实现多 Session 列表、会话切换、分叉、过期或多通道共享历史。

### 存储位置

默认目录：

```text
~/.mini-harness/
├── config.json                          # Provider 配置（可能含 API Key，0600）
└── sessions/
    └── <workspace-id>/
        └── session.json                 # 当前 Workspace 的默认 Session
```

Session 历史**不写入 Workspace**，避免 Agent 的工作文件和 Harness 内部状态混在一起。`--workspace` 只决定读取哪个 Session 目录，不会改变 Session 的存储位置。

可通过 `MINI_HARNESS_HOME` 覆盖 Harness 数据根目录；`data_root` 也可以由测试或嵌入式调用显式传入。

### JSON 格式

```json
{
  "schema_version": 1,
  "workspace": "/path/to/project",
  "session_key": "cli:main",
  "messages": [
    {
      "role": "user",
      "content": "计算 2 + 3"
    },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_001",
          "name": "add",
          "arguments": {"a": 2, "b": 3}
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_001",
      "name": "add",
      "content": "5"
    },
    {
      "role": "assistant",
      "content": "结果是 5"
    }
  ]
}
```

字段与当前 V0 的消息协议一致。要求包括：

- UTF-8 编码，`ensure_ascii=False`。
- 保留 assistant 的 `tool_calls` 和 tool 的 `tool_call_id`。
- 不保存 API Key、Provider 密钥或运行配置。
- 不使用 pickle 反序列化。
- 不存在文件时返回空 Session；存在时自动加载。
- JSON 损坏时抛出明确错误，绝不静默覆盖原历史。
- Workspace 路径不匹配时拒绝错误恢复。

### 原子保存

`SessionManager.save()` 的写入顺序：

```text
序列化 JSON
    ↓
在 session.json 同目录创建临时文件
    ↓
写入 + flush + fsync
    ↓
chmod 0600
    ↓
os.replace() 原子替换
    ↓
尽量 fsync 父目录
```

发生异常时清理临时文件，并尽量保持旧 `session.json` 完整。`SessionManager.clear()` 通过保存空 `messages` 实现，因此清空后重启仍然为空。

### 并发与恢复限制

V1 明确是**单进程单写者**模型：

- CLI 启动时会在 macOS/Linux 对当前 Workspace 的 `session.lock` 加非阻塞 `flock`；同一台机器上的第二个 CLI 实例会收到明确的 `SessionInUseError`，不会静默并发覆盖。Windows 当前没有 `fcntl`，退化为不锁，仍需按单写者使用。
- `flock` 是建议锁，不是数据库事务；直接调用 `SessionManager` 的程序不在锁保护范围内。
- 不支持跨进程强一致写入、分布式锁、多写者合并或运行中消息注入。
- 原子替换保护的是“单次保存不留下半个文件”，不等同于完整事务或掉电恢复保证。
- 加载到损坏 JSON 时会拒绝恢复，不会自动重建或覆盖原文件。

## 交互式 CLI

### 启动

```bash
cd /path/to/project
mini-harness
```

首次启动或没有默认 Provider 配置时，先选择 Provider、模型和 Base URL，并按提示输入 API Key；创建配置时默认保存为默认配置。之后启动会直接使用默认配置，不再显示选择菜单。

保存配置位于 `~/.mini-harness/config.json`，权限为 `0600`。API Key 只保存在该配置文件，不会写入 Session JSON。

### 命令

| 命令 | 作用 |
|---|---|
| `/help` | 查看命令帮助 |
| `/model` | 切换 Provider / 模型（保留当前 Session 历史） |
| `/config` | `/model` 的别名 |
| `/history` | 查看当前 Session 的历史 |
| `/clear` | 清空当前 Session 的内存状态和磁盘 JSON |
| `/tools` | 查看已注册工具 |
| `/exit` 或 `/quit` | 安全退出 |

这些以 `/` 开头的命令由 CLI 本地处理，不会发送给 LLM。用户、助手、工具和错误使用不同颜色；设置 `MINI_HARNESS_NO_COLOR=1` 可关闭颜色。

## Provider 支持

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
mini-harness --interactive
```

华为云 MaaS 的 Base URL 以控制台提供的 OpenAI-compatible 地址为准，预设中不硬编码；CLI 会要求输入 Base URL，也可以预先设置 `HUAWEI_MAAS_BASE_URL` 或 `HUAWEI_MAAS_DIRECT_BASE_URL`。模型列表中已包含截图中的 `DeepSeek-v4.1-Flash`、`GLM-5.3`、`Qwen-300` 和 `deepseek-v4.1-flash-1017`。自定义 OpenAI-compatible 也可以用 `MINI_HARNESS_BASE_URL` 提供 Base URL。

如果自定义端点返回 `Failed to get the authorization header` 或 HTTP 401/403，说明服务端要求鉴权：请填写 API Key，或设置 `MINI_HARNESS_API_KEY`。本地无鉴权端点可以留空；需要非 Bearer 的额外 Header 时，可通过代码中的 `ProviderConfig(extra_headers={...})` 配置。

### 预设列表

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

```python
from mini_harness.providers import ProviderConfig, ProviderFactory

provider = ProviderFactory().create(
    ProviderConfig(provider="deepseek", model="deepseek-chat")
)
```

API Key 默认从 `DEEPSEEK_API_KEY` 读取，也可以显式传入 `api_key`。

## 与 Nanobot 的对应关系

| Mini Harness V1 | Nanobot |
|---|---|
| Workspace 路径解析 | Workspace 及路径配置 |
| Workspace ID | `session/location.py` |
| Session | `session/manager.py` |
| SessionManager | `SessionManager` / `SessionState` |
| JSON 持久化 | `SqliteSessionStore`（正式实现为 SQLite） |
| 加载历史 | `Session.get_history()` |
| AgentLoop 接入 | `_process_message()` / `_build_turn()` |
| 保存历史 | `_persist_turn()` / `finish_turn()` |

需要明确区分：

- **V1 实际实现：** 单 CLI Channel、Workspace 隔离、单默认 Session、JSON 文件持久化、原子替换、CLI 单实例建议锁、多轮历史恢复。
- **Nanobot 正式源码的高级能力：** 多 Session、Session Queue、Session Lock、更完整的并发/一致性控制、SQLite/数据库存储、ContextBuilder、上下文压缩、Memory、Hooks、Checkpoint、MCP、多通道等。
- **V1 不支持：** 多 Session 切换、会话分叉/过期、跨进程并发写入保证、SQLite、ContextBuilder、Memory、Hooks、Checkpoint、MCP、多 Channel、流式响应、运行中消息注入。

本实现不声称与 Nanobot 在 Session 并发、一致性或恢复能力上完全等价。

## 项目结构

```text
mini-harness/
├── main.py                       # 兼容入口：交互式 / --demo / --list-providers
├── pyproject.toml
├── uv.lock
├── README.md
├── mini_harness/
│   ├── __init__.py
│   ├── __main__.py
│   ├── workspace.py              # Workspace / WorkspaceResolver / workspace_id
│   ├── bus/
│   │   ├── events.py
│   │   └── queue.py
│   ├── agent/
│   │   ├── loop.py               # Session 感知的 MessageBus 消费者
│   │   ├── prompt.py             # 最小 System Prompt
│   │   └── runner.py
│   ├── session/
│   │   ├── __init__.py
│   │   ├── manager.py            # Session / SessionManager / JSON 原子持久化
│   │   └── workspace.py          # Workspace 的 session 子包导出
│   ├── cli/
│   │   ├── app.py
│   │   ├── main.py
│   │   └── profiles.py
│   ├── providers/
│   │   ├── base.py
│   │   ├── config.py
│   │   ├── presets.py
│   │   ├── factory.py
│   │   ├── openai_compat.py
│   │   ├── anthropic.py
│   │   └── mock.py
│   └── tools/
│       ├── base.py
│       ├── filesystem.py         # GrepTool / ListDirTool / ReadFileTool / WriteFileTool
│       └── registry.py
└── tests/
    ├── test_bus.py
    ├── test_runner.py
    ├── test_prompt.py
    ├── test_e2e.py
    ├── test_providers.py
    ├── test_cli.py
    ├── test_workspace.py
    ├── test_session.py
    ├── test_session_integration.py
    └── test_filesystem_tools.py
```

## 测试

```bash
uv run pytest -q
```

当前结果：

```text
97 passed
```

测试覆盖：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_bus.py` | 入站/出站消息往返、asyncio.Queue FIFO |
| `tests/test_prompt.py` | System Prompt 的 Workspace 路径和关键规则 |
| `tests/test_runner.py` | MockProvider、消息历史、tool_call_id 配对、max_iterations、预算耗尽收尾、System Prompt 隔离、参数校验、工具异常 |
| `tests/test_e2e.py` | V0 完整 E2E 调用链、CLI 演示输出、Loop 正常退出与停止时序 |
| `tests/test_providers.py` | 预设完整性、工厂构建、环境变量、OpenAI/Anthropic 请求响应转换、HTTP 错误 |
| `tests/test_cli.py` | Profile 存储与权限、API Key 持久化、默认配置、legacy max_tokens 迁移、Mock 多轮对话、`/model` 切换、Provider 目录 |
| `tests/test_workspace.py` | 稳定 workspace_id、不同路径隔离、`../`、绝对路径和符号链接越界检查 |
| `tests/test_session.py` | 缺失/损坏 JSON、Workspace 冲突、独立副本、清空、原子保存失败、tool_calls 持久化、单实例锁 |
| `tests/test_session_integration.py` | Session 重启恢复、不同 Workspace 隔离、历史不重复、第二轮 Provider 入参精确包含 system/user/assistant/tool_calls/tool_result、持久化失败、预算耗尽收尾落盘、失败回合不落盘、CLI 命令 |
| `tests/test_filesystem_tools.py` | `list_dir`/`grep`/`read_file`/`write_file` 的递归发现、噪声目录过滤、正则搜索、路径校验、分页、UTF-8/二进制错误、Markdown 创建与覆盖、CLI 工具注册 |

## V1 已实现功能与限制

### 已实现

- [x] Workspace 解析、固定、稳定 ID 和 Session 存储映射
- [x] 单 Channel、单默认 Session 的加载、保存与清空
- [x] JSON + `schema_version`、UTF-8、`ensure_ascii=False`
- [x] 同目录临时文件 + `flush` + `fsync` + `os.replace` 的原子保存
- [x] JSON 损坏、Workspace 不匹配、保存失败时明确报错并保留旧文件
- [x] AgentLoop 接入 Session，并校验 Runner 返回历史的前缀关系
- [x] CLI 启动摘要、`/history`、`/clear`、`/exit`
- [x] CLI 单实例 `flock` 建议锁
- [x] `list_dir` / `grep` / `read_file` / `write_file` 文本工具，使用 Workspace 路径策略
- [x] 最小 System Prompt、默认 32 轮预算、预算耗尽收尾
- [x] `max_tokens` 默认 4096 与旧配置迁移
- [x] 失败回合不写入 Session
- [x] V0 原有测试与 V1 新增测试全部通过

### 限制

- Workspace 重命名/移动会产生新 `workspace_id`，不自动迁移旧 Session。
- V1 只有一个 CLI Channel、一个默认 Session，不支持多 Session 切换。
- JSON 持久化是单进程单写者方案；`flock` 只保护 CLI，不保护任意库调用。
- 不做 ContextBuilder、上下文裁剪或压缩；历史按原样传给模型。
- 没有长期 Memory、Hooks、Checkpoint、MCP、多通道和运行中消息注入。
- 文件工具只支持 UTF-8 文本/Markdown，不支持图片、PDF、Office、`edit_file` 或 shell 命令。
- 路径检查不是完整安全沙箱，符号链接竞态和并发文件系统修改是后续强化项。
- 没有 SQLite；V1 的 JSON 存储不适合大规模或高并发使用。

## 后续建议（V2，不在本次实现）

1. **ContextBuilder**：按 Token 预算整理历史，区分 system、history、current turn，处理长对话截断与压缩。
2. **Session Queue / 并发控制**：每个 Session 串行处理回合，为多客户端共享同一 Session 做准备。
3. **SQLite SessionStore**：替换 JSON 文件，引入事务、索引和更可靠的恢复能力。
4. **更完整的文件工具**：在现有 `list_dir`/`grep`/`read_file`/`write_file` 上增加 `edit_file`、图片/PDF/Office 读取和符号链接竞态防护。
5. **Memory / Skills / Hooks / Checkpoint / MCP**：按 Nanobot 的产品层能力逐层增加，不在 V1 提前实现。

## 安全提示

- API Key 以明文保存在本机 `~/.mini-harness/config.json`，文件权限为 `0600`；不要提交到版本库或共享给他人。
- Session JSON 不保存 API Key，但仍可能包含用户对话和工具输出，请按需保护本地数据目录。
- 不要把 `Workspace.resolve_path()` 当成完整沙箱；生产环境需要更严格的路径和文件描述符级防护。

## 参考

- [HKUDS/nanobot](https://github.com/HKUDS/nanobot)
- [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat)
- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages)
- Python `asyncio` 文档：https://docs.python.org/3/library/asyncio.html
