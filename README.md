# Mini Harness

Mini Harness 是一个基于 Python `asyncio` 构建的最小 Agent Harness，用于从零理解真实 Agent Harness 的分层设计：

```text
CLI → InboundMessage → MessageBus → AgentLoop
    → SessionManager.load() → ContextBuilder → request_messages
    → AgentRunner → LLMProvider / ToolRegistry → ToolResult → AgentRunner
    → AgentRunResult → SessionManager.save()
    → OutboundMessage → MessageBus → CLI
```

项目参考 [HKUDS/nanobot](https://github.com/HKUDS/nanobot) 的分层思路，但有意保持轻量：不依赖 Agent 框架、不使用数据库，只保留消息总线、Agent Loop、Agent Runner、Provider、ToolRegistry、Session/Workspace 产品层和 V2 的 ContextBuilder。

当前版本 **Mini Harness V2** 在 V0 工具调用闭环与 V1 Session/Workspace 之上，增加了 Context Engineering：

- 集中维护的默认 System Prompt（`mini_harness/prompts/defaults.py`）
- Workspace 根目录 `AGENTS.md` 自动加载（UTF-8、大小限制、失败降级）
- 独立 `ContextBuilder`：System Prompt + 历史选择 + 当前消息 → `request_messages`
- 基于 Token 估算的上下文预算（`max_context_tokens` / `reserve_output_tokens`）
- 按完整用户轮次裁剪历史，保持 ToolCall / ToolResult 配对
- 最近完成轮次关键结论保护：整轮超预算时生成确定性“完成轮次摘要”，不会因大量工具输出丢掉最终 Assistant 结论
- 每次 Provider 请求前的 ContextGovernance：检查 System + messages + Tool Schema + 输出预留，支持软/硬阈值
- 轻量级 `ContextCompactor`：对已完成旧历史用现有 Provider 生成结构化摘要；当前用户请求与进行中的工具链原样保留
- 持久化 Session Summary Checkpoint：原始历史完整保留，归档边界与摘要跨进程恢复，请求不再重复发送已压缩历史
- `memory/` 长期记忆：`MEMORY.md` + `history.jsonl` + `.dream_cursor`，每次持久化压缩后归档摘要
- Idle Compaction（默认 15 分钟空闲、60 秒检查）与 `/compact` 手动压缩
- Dream 长期记忆整理（默认每 2 小时检查、单次最多 20 条）与 `/dream` 手动整理，结构化操作 + 去重 + 原子写入
- 大型 Tool Result 在 Provider 上下文中按 Token 截断并保留 path/offset/pattern 等元数据，Session 中的原始结果仍完整
- `AgentRunResult.new_messages` 显式边界，避免 System Prompt 或裁剪后历史污染 Session
- `/context` CLI 调试命令，展示真实 Token 预算与选择结果；`/last-prompt` 展示最后一次真实 Provider 请求快照
- V1 的 Workspace、Session 持久化、多轮恢复、流式输出和全部 V0 能力保持不变

## 当前状态

- 可运行、可测试的 Agent 工具调用闭环。
- Provider 工厂、厂商预设、OpenAI-compatible 与 Anthropic 两种 HTTP 协议适配器。
- 内置 23 个 Provider 预设，覆盖国内外厂商、第三方聚合平台、本地 Ollama/LM Studio 和自定义 OpenAI-compatible 服务。
- 内置离线 `MockProvider`，无需网络和 API Key 即可体验完整工具调用流程。
- V1/V2 Session 历史保存在 Harness 数据目录，不写入用户 Workspace；System Prompt 与 AGENTS.md 不写入 Session。
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

# 按实际模型能力配置上下文预算；新 profile 会保存这些能力字段
mini-harness --interactive --context-window-tokens 131072 --max-output-tokens 32768
# 也可直接覆盖本次运行的输入窗口估算
mini-harness --interactive --max-context-tokens 32768 --reserve-output-tokens 4096

# 关闭流式输出，等待完整回答后一次性打印
mini-harness --interactive --no-stream

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

## V2 架构

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
ContextBuilder.build_context(history, current_message)
    ├── build_default_system_prompt(workspace)
    ├── load AGENTS.md（每轮重新读取）
    ├── select history（完整用户轮次 + 工具配对校验）
    └── Token 预算估算
    ↓
request_messages = [system, *selected_history, current_user_message]
    ↓
AgentRunner: LLM → Tool → LLM（不感知 Session / AGENTS.md）
    ↓
AgentRunResult
    ├── messages = request_messages + new_messages
    └── new_messages / input_message_count（显式边界）
    ↓
Session.messages += current_message + new_messages（完整旧历史保留）
    ↓
SessionManager.save()（原子写入）
    ↓
OutboundMessage
    ↓
MessageBus
    ↓
CLI 显示回答
```

`--demo` 仍然走 V0 的 MessageBus + AgentLoop 路径，只是不启用 Session 与 ContextBuilder。

### 模块职责

| 模块 | 职责 |
|---|---|
| `workspace.py` | 解析并固定 Workspace，计算稳定 ID，提供路径边界检查 |
| `session/manager.py` | 定义 `Session`，负责 JSON 读取、原子保存和清空；只保存完整历史 |
| `agent/loop.py` | 消费 `InboundMessage`，加载 Session，调用 ContextBuilder，执行 Runner，只追加本轮新增消息 |
| `agent/context.py` | V2 ContextBuilder：System Prompt 组装、AGENTS.md 读取、Token 估算、历史选择与预算 |
| `agent/prompt.py` | V1 兼容导出；实际默认模板集中在 `prompts/defaults.py` |
| `prompts/defaults.py` | 唯一的默认 System Prompt 来源 |
| `agent/runner.py` | 只执行模型/工具迭代，不感知 Session 文件或 Workspace；返回 `new_messages` 显式边界 |
| `bus/` | 隔离输入输出通道与 Agent 核心 |
| `cli/app.py` | 配置选择、CLI 命令、`/context` 和终端展示 |
| `tools/` | 工具注册、参数校验和执行；包含加法、文件读写/搜索/编辑/元信息，以及命令执行 |

Session 持久化代码没有放入 `AgentRunner` 或 `ToolRegistry`。V2 进一步明确：Session 保存历史、ContextBuilder 构建模型输入、AgentRunner 执行；三者不混合职责。

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
Mini Harness V2
Workspace: /private/tmp
Session: cli:main
History: 4 messages
Loaded messages: 4
Context budget: 7168 input tokens (max 8192, reserve 1024)
```

### Workspace ID

V1/V2 使用规范化绝对路径计算稳定 ID：

```python
workspace_id = hashlib.sha256(
    str(workspace.resolve()).encode("utf-8")
).hexdigest()[:16]
```

- 同一路径在不同进程启动后得到相同 ID。
- 不同路径得到不同 ID。
- 不使用随机 UUID。
- Session 文件同时记录完整 Workspace 路径，加载时校验冲突。

**V2 已知限制：** Workspace 被重命名或移动后，绝对路径变化会产生新的 ID，旧 Session 不会自动迁移。Nanobot 的持久 workspace-id 标记、路径迁移和复制检测不在 V2 实现范围。

### Workspace 文件路径策略

`list_dir`、`grep`、`read_file`、`write_file` 和 `edit_file` 都通过 `Workspace.resolve_path()` 解析路径，并处理：

- `../` 路径穿越
- Workspace 外的绝对路径
- 已存在且指向 Workspace 外部的符号链接

示例：

```python
workspace = WorkspaceResolver().resolve()
path = workspace.resolve_path("README.md")  # /workspace/README.md
```

文本工具支持 UTF-8：`read_file` 返回带行号的内容，`write_file` 支持覆盖和追加，`edit_file` 做精确替换。`file_info` 可以检查二进制文件的元信息。文件工具不会改变进程当前工作目录，路径按 Workspace 校验。

**安全说明：** `Path.resolve()` + `relative_to()` 不是完整的安全沙箱。路径检查与实际打开文件之间仍可能存在并发替换符号链接的竞态，V2 将这一点明确列为后续安全强化项，而不是宣称提供了完整文件系统隔离。

### 文件工具

在 Workspace 内提供以下文件工具：

```text
list_dir(path, recursive=False, max_entries=200)
find_files(pattern, path=".", kind="file", max_results=200)
grep(pattern, path=".", glob="*", max_matches=100)
read_file(path, offset=1, limit=200)
write_file(path, content, mode="overwrite")
edit_file(path, old_text, new_text, replace_all=False)
apply_patch(edits=[{"path": "app.py", "old_text": "before", "new_text": "after"}])
file_info(path)
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

`find_files`：

- 按文件名或相对路径 glob 查找，例如 `*.py`、`src/**/*.py`
- 支持 `kind="file"|"dir"|"any"`，默认最多返回 200 项
- 禁止绝对模式和 `..`，过滤工作区外的链接，不递归目录符号链接
- 与 `list_dir`、`grep` 共用遍历逻辑，在进入噪声目录之前剪枝

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
- `mode="append"` 只把 `content` 追加到文件末尾，不自动补换行、不重写原内容；不存在时创建文件，已有权限保持不变。追加不提供覆盖模式的原子替换保证。

`file_info` 返回 JSON：`type`、`size_bytes`、UTC `mtime`、`is_binary`、`line_count` 和 `sampled`。对普通文本计算完整行数；超过 2 MiB 只检测前缀，`line_count=null`，避免为元信息加载整个大文件。二进制检测采用 UTF-8/NUL 启发式，采样结果不代表整份文件。

`edit_file`：

- 在已有 UTF-8 文本文件中做精确字符串替换
- 默认要求 `old_text` 只能出现一次，避免误改
- `replace_all=true` 时替换所有匹配
- 使用 `read_file` 读取精确文本后再编辑；新建/完整重写请用 `write_file`
- 保留原文件的 CRLF/LF 换行风格和权限位

`apply_patch` 接受多个精确替换的 `edits` 数组，不接收 unified diff。同一文件
按数组顺序叠加，全部通过后才写入；单次和批量编辑共用同一套 UTF-8、匹配、
换行和权限处理。输入及替换结果每文件最多 2 MiB，替换前先检查结果大小。
写入阶段失败会尝试恢复原字节；恢复失败会明确报告仍可能改变的文件，不提供
进程崩溃时的跨文件原子提交。

工具实现按职责组织：

| 模块 | 职责 |
| --- | --- |
| `tools/filesystem.py` | `read_file`、`write_file`、`file_info` |
| `tools/search.py` | `list_dir`、`find_files`、`grep`，共享遍历、过滤和边界规则 |
| `tools/edit.py` | `edit_file`、`apply_patch`，共享编辑与提交逻辑 |
| `tools/_file_io.py` | 低层文件大小限制、保权限的原子写入，不依赖具体工具 |
| `tools/command.py` | 命令执行及进程管理 |

统一通过 `mini_harness.tools` 导入公开工具；需要按实现模块导入时，搜索工具
使用 `mini_harness.tools.search`，编辑工具使用 `mini_harness.tools.edit`。

文件工具只在 CLI 有 Workspace 时注册；V0 无 Workspace 的兼容调用仍只有 `AddTool`。文件工具需要真实 Provider 具备 tool calling 能力；内置 `MockProvider` 是预设脚本，默认 `mock-add-demo` 仍只会调用 `add`。

### 命令执行

```text
run_command(command, cwd=".", timeout=30)
```

CLI 有 Workspace 时自动注册。`cwd` 必须是 Workspace 内的现有目录；合并 stdout/stderr，最多保留前 16 KiB，并持续排空管道以免大输出阻塞。返回退出码，非零退出或超时记为工具错误。最长运行 120 秒；超时、取消和正常退出后都会清理 POSIX 进程组，防止后台子进程遗留。其他系统仅清理直接进程。

常见 `rm`、`sudo`、`git reset --hard` 等命令通过宿主确认钩子；CLI 要求用户输入 `yes`，库调用未配置钩子时拒绝此类命令。检测只覆盖常见写法，**cwd 边界与危险命令检测都不是 shell 沙箱**：命令仍拥有当前用户的文件和网络权限。

`ToolRegistry(timeout_seconds=130, max_output_chars=32000)` 为所有异步工具提供取消式超时和统一输出限制。同步阻塞的第三方工具需要自行使用异步 I/O 或进程隔离，事件循环超时不能抢占同步 Python 代码。

### System Prompt、AGENTS.md 与上下文预算（V2）

V2 的 System Prompt 由 `ContextBuilder.build_system_prompt()` 每轮重新构建，模板的唯一来源是 `mini_harness/prompts/defaults.py`：

1. 默认 System Prompt 包含 Workspace 路径、工具使用规则、不得编造工具结果等约束；
2. 若 Workspace 根目录存在 `AGENTS.md`，其内容以 `# Workspace Instructions (AGENTS.md)` 段落追加；
3. System Prompt 只存在于临时 `request_messages`（以及 `AgentRunResult.messages` 的输入前缀）中，不属于本轮 `new_messages`，绝不写入 Session。

AGENTS.md 的 V2 规则：

- 只读取 `WorkspaceResolver` 固定下来的 Workspace 根目录 `AGENTS.md`，不递归查找、不合并多份；
- UTF-8 读取；空文件忽略；
- 默认大小上限 16 KiB（`ContextConfig.agents_md_max_bytes`）；超过限制整份跳过，不输出半份规则；
- 读取失败（权限、编码）记录 warning 并降级为默认 System Prompt，不打断对话；
- 每轮重新读取，文件修改后下一轮立即生效；
- 不自动修改 AGENTS.md，不把内容写入 Session。

运行预算（`ContextConfig`，均可配置）：

- `max_context_tokens`：模型上下文窗口 `C` 的估算值；未配置模型能力时保留教学默认 8192；
- `reserve_output_tokens`：最低输出预留；未知模型沿用教学默认 1024，已配置模型窗口时默认至多预留 4096，不把最大输出能力全部永久扣除；
- 实际接入模型时使用 `ProviderConfig.context_window_tokens` / `max_output_tokens`，CLI 能力参数同时覆盖已保存配置和新建配置；
- 可用输入预算 `= C - reserve_output_tokens`（未知模型教学默认 7168）；
- 每次 Provider 请求再计算 `O_effective = min(O_max, C - I - M)`，作为本次实际发送的 `max_tokens`；输入增多时输出额度相应减少，输入减少后可恢复至模型输出上限；

- 再从可用输入预算中扣除 System Prompt、当前用户消息、Tool Schema 估算和消息结构开销，剩余部分才是历史预算；
- `AgentRunSpec.max_iterations` 默认 32，CLI 支持 `--max-iterations 50`；
- 预算耗尽时 `finalize_on_max_iterations` 会再请求一次不带工具的总结，停止原因记为 `max_iterations_finalized`；
- 已执行过工具的失败回合会保存当前用户消息与执行记录；纯失败且无工具副作用的回合不写入 Session。

Token 估算使用简化算法（ASCII 约 4 字符/Token，非 ASCII 约 1 字符/Token），并额外计算角色、消息结构、tool_calls 与 Tool Schema 开销。**估算 Token 不等于模型真实 Token 数**，`reserve_output_tokens` 同时承担估算误差的安全余量。可用 `/context` 查看真实构建统计。

如果 System Prompt + 当前用户消息 + 必要开销已经超过输入预算，`ContextBuilder` 会抛出 `ContextBudgetExceeded`，AgentLoop 返回 `context_budget_exceeded` 错误；不会截断当前用户输入，不会清空 Session 历史，也不会调用 Provider。

### 自动 Compact 与每次请求预算治理（V2.1）

V2.1 在 `AgentRunner` 调用 Provider 的统一入口增加 `ContextGovernor`：第一次请求和后续每一次工具循环请求都会重新检查预算（System Prompt + messages + Tool Schema + 输出预留 + 安全余量）。达到硬阈值时先调用 `ContextCompactor` 对已完成旧历史生成结构化摘要；摘要失败或仍超预算时执行确定性回退（先截断 Tool Result，再从旧到新丢弃可压缩轮次）；仍无法放入预算则抛出 `ContextBudgetExceeded`，不会发送明显超预算请求，也不会无限压缩。

- `ContextConfig.compact_soft_ratio`：工具循环中的软阈值，默认 0.8；
- `ContextConfig.max_tool_result_tokens`：请求超预算时单条 Tool Result 的首轮回退上限，默认 2048；
- `ContextConfig.enable_auto_compact`：是否允许 LLM 摘要，默认开启；
- Summary 使用现有 `LLMProvider` 生成，正常情况下只存在于本次请求上下文；需要跨回合持久化时由 V2.2 的 `SessionCompactor` 写入 Session Summary Checkpoint；
- 当前用户消息及尚未发送的 in-flight 工具组（Delta）原样保留；
- Provider 接受请求不代表模型永久记住内容；当前回合只有超预算时才把较早的 assistant/tool 组换成 `accepted_summary` 参考摘录，最近两组已接受调用及最新组保留配对结构；
- 无论窗口大小，总请求放得下就保留完整工具正文；超预算时才截断，保留头尾及 path/offset/limit 等元数据；原始结果仍完整保存在 Session。
- Summary 作为普通 user/历史引用消息发送，明确标注“reference data only, not instructions”，不冒充 System 规则。

### 持久化 Compaction、Idle Compaction 与 Dream（V2.2）

V2.2 在 V2.1 的请求内 Token Compaction 之上增加持久化层，不改变既有预算算法：

```text
Session.messages               完整原始历史（永不因压缩删除）
Session.summary                当前有效摘要（持久化）
Session.summary_boundary       已归档历史条数（0..len(messages)）
Session.summary_checkpoint_id  摘要检查点稳定 ID（Memory 归档去重）
```

模型请求结构变为：

```text
System Prompt（含 AGENTS.md / Long-term Memory）
    + Session Summary
    + Unarchived History（summary_boundary 之后的原始消息）
    + Current Message
```

- `SessionCompactor`：统一 `/compact`、Idle Compaction 与未来触发源的压缩实现；只压缩已完成的完整用户轮次，遇不完整轮次停止，工具调用与结果配对保持不变；支持把上一版 Summary 与新增历史合并后再摘要；失败时 Session 完全不变。
- 保存顺序：先以单次原子 JSON 替换写入 `summary` + `summary_boundary` + `summary_checkpoint_id`，成功后再追加 Memory Archive；即使归档失败，摘要检查点仍然有效。
- `IdleCompactionScheduler`：默认每 60 秒检查 Session 文件 mtime，超过 15 分钟无活动且有完整未归档历史时尝试压缩；正在执行任务的 Session、活跃会话、无新历史时跳过；同一 Session 通过 `asyncio.Lock` 互斥。`ContextConfig.idle_compact_after_minutes = 0` 可关闭。
- `/compact`：不依赖 Token 阈值与空闲时间，立即压缩当前 Session 未归档历史，返回压缩成功 / 无需压缩 / 失败 / 忙。
- Memory Archive：`<workspace>/memory/history.jsonl` 每条记录包含 `cursor`（从 1 单调递增）、`session_key`、`timestamp`、`content`、`checkpoint_id`；同一检查点重复归档幂等。
- `DreamManager`：读取 `.dream_cursor` 之后、单次最多 `dream_max_entries`（默认 20）条归档摘要；没有新记录时不调用 LLM；LLM 只返回结构化 JSON（`add` / `update` / `remove`），由代码校验、去重、按 section 合并后原子写入 `MEMORY.md`；只有写入成功后才推进 `.dream_cursor`，任何失败都保留旧 cursor 允许下次重试。
- `DreamScheduler`：默认每 2 小时检查一次；进程退出即停止，重启后从 cursor 继续。
- `/dream`：手动触发同一 Dream 提取逻辑，与定时任务通过实例锁互斥；返回处理条数、新增/更新/删除/跳过数量、cursor 与失败原因。
- `/clear` 只清空当前 Session（含 Summary Checkpoint），不删除 Workspace 的长期记忆 `MEMORY.md`。
- CLI 配置：`--idle-compact-after-minutes 0` 关闭空闲压缩，`--dream-interval-hours N` 调整自动 Dream 间隔，`--no-dream` 仅关闭自动 Dream（`/dream` 手动整理仍可用）。

最近一个已完成轮次整轮放不进历史预算时，`ContextBuilder` 会生成确定性的“完成轮次摘要”（用户请求 + 工具名 + 最终 Assistant 结论），确保 P0-1/P0-2 这类关键结论不会跟着大型工具输出一起被丢弃。

`/last-prompt` 展示的是 `AgentRunner` 在真实 Provider 调用边界记录的快照：请求类型（normal/finalize）、iteration、估算 Token、实际输出上限、accepted 边界、是否发生 Compact、工具消息数、实际 messages 与 Tool Schema；不包含 API Key 等认证信息。

Runner 会在连续只读、相同参数重复读取和临近迭代上限时加入临时进度提示；提示只进入模型请求，不保存到 Session，也不禁止文件修改后的合理重读。项目分析只在用户要求保存报告时创建文件。问题复盘与验证见 [重复读取问题修复](docs/agent_read_loop_fix.md)。

连接或切换模型时立即显示有效窗口、输入预算、输出上限和请求的推理档位，首次 `/context` 即可核对。模型窗口未配置时明确提示使用 8K 回退值。`--max-context-tokens` 和 `--reserve-output-tokens` 分别覆盖窗口与最低输出预留，不会因为只指定预留而把大窗口重置为 8K。

例如，端点确认支持 1M 上下文、384K 输出和最高推理档位时：

```bash
mini-bot --interactive --context-window-tokens 1048576 --max-output-tokens 393216 --thinking --reasoning-effort max
```

参数仅覆盖本次连接；要长期使用，可保存相应 profile 的 `context_window_tokens`、`max_output_tokens`、`thinking` 和 `reasoning_effort`。千问AI/DashScope 的思考开关使用 `enable_thinking=true`，DeepSeek 原生格式使用 `thinking={"type":"enabled"}`。不要把某个模型的额度直接用于其他模型或端点。

### 输出长度

`ProviderConfig`、`ProviderProfile` 和 Provider 构造参数统一使用 `max_output_tokens`，
默认 4096，必须为正整数。显式配置 1024 会保留 1024，不再自动升级；0 不表示
默认值。命令行省略 `--max-output-tokens` 表示不覆盖已保存配置，显式传值必须大于 0。

Provider 的 `max_output_tokens` 保存配置能力；`output_token_limit` 保存当前请求的
有效额度，Governor 通过 `set_output_budget()` 更新后者，不修改配置上限，也不
依赖首次请求时缓存的隐藏原值。协议适配器将有效额度映射到 API 的 `max_tokens`
或 `max_completion_tokens`，这些协议字段不是本地配置字段。

已移除本地配置的 `max_tokens` 字段。加载该字段或非法 `max_output_tokens` 会抛出
`ProfileStoreError` 并在 CLI 显示修改提示，不修改磁盘或静默删除配置。手工更新
旧文件时，保留正确的输出上限到 `max_output_tokens`，再删除 `max_tokens`；原先
`max_output_tokens=0` 表示未设置的记录应改为明确的正整数。

## Session

### 数据结构

```python
@dataclass(slots=True)
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
```

V1/V2 固定默认 Session Key：

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

V2 明确是**单进程单写者**模型：

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

OpenAI-compatible 配置在新建时可选择推理强度。聊天中输入 `/settings` 可修改当前模型：DeepSeek V4.1 提供 `low`、`high`、`max`，支持输入编号或档位名；`0` 恢复端点默认，回车保留当前值，`q` 取消。自定义档位按端点支持值填写。选择档位后显式请求开启思考。

已有命名配置会自动保存推理设置，并立即用于下一次请求；未保存配置仅在当前连接生效。修改保留会话历史、API Key、模型和上下文/输出额度。启动时的 `--reasoning-effort` 仍可覆盖保存值。当前推理强度菜单适用于 OpenAI-compatible 适配器。

保存配置位于 `~/.mini-harness/config.json`，权限为 `0600`。API Key 只保存在该配置文件，不会写入 Session JSON。

### 命令

| 命令 | 作用 |
|---|---|
| `/help` | 查看命令帮助 |
| `/model` | 切换 Provider / 模型（保留当前 Session 历史） |
| `/config` | `/model` 的别名 |
| `/settings` | 设置当前模型的推理强度，已有命名配置自动保存 |
| `/history` | 查看当前 Session 的历史 |
| `/context` | 查看当前上下文结构、AGENTS.md 状态、Token 预算与历史选择统计（不调用 LLM、不保存 Session） |
| `/last-prompt` | 查看当前进程最近一次真正发给模型的完整请求消息与工具定义（不调用 LLM、不保存 Session）；进程重启后失效 |
| `/clear` | 清空当前 Session 的内存状态和磁盘 JSON |
| `/tools` | 查看已注册工具 |
| `/exit` 或 `/quit` | 安全退出 |

这些以 `/` 开头的命令由 CLI 本地处理，不会发送给 LLM。用户、助手、工具和错误使用不同颜色；设置 `MINI_HARNESS_NO_COLOR=1` 可关闭颜色。

### 流式输出

交互式 CLI 默认开启流式输出；`--no-stream` 可回退到一次性打印。流式链路为：

```text
InteractiveCLI(stream=True)
    → StreamingAssistantRenderer
    → AgentLoop(on_text_delta, on_tool_start)
    → AgentRunSpec
    → AgentRunner._request
    → Provider.chat_stream(...)
        → OpenAI-compatible: stream=true + SSE delta.content
        → Anthropic: text_delta / input_json_delta
    → Console.write_stream_delta
    → 完整 LLMResponse 返回 Runner
    → tool_calls / 消息历史 / Session 保存逻辑保持不变
```

流式只影响终端展示：中间文本 chunk 不会写入 `result.messages`；工具调用的分片参数会先聚合完整，再执行工具。最终回答已经流式显示时，CLI 通过 `AgentRunResult.streamed_final` 避免重复打印。

注意流式输出不会改变 `max_tokens` 上限；它只是把单次回复拆成多个 chunk 传输。

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

| Mini Harness V2 | Nanobot |
|---|---|
| Workspace 路径解析 | Workspace 及路径配置 |
| Workspace ID | `session/location.py` |
| Session | `session/manager.py` |
| SessionManager | `SessionManager` / `SessionState` |
| JSON 持久化 | `SqliteSessionStore`（正式实现为 SQLite） |
| 加载历史 | `Session.get_history()` |
| `ContextBuilder.build_system_prompt()` | 同名函数：identity + bootstrap files + tool contract + memory + skills |
| `ContextBuilder.build_context()` | `ContextBuilder.build_transcript()` / `TranscriptInput` |
| 根目录 AGENTS.md 加载 | `_load_bootstrap_files()`（AGENTS.md / SOUL.md / USER.md） |
| token 估算 + 最近完整轮次选择 | `Session.get_history()` 的 `max_tokens` / `max_messages` + `find_legal_message_start()` |
| `ContextBudgetExceeded` | `ContextWindowExceededError` / `ContextGovernor.ensure_request_fits()` |
| `AgentRunResult.new_messages` | Nanobot 由 Session 状态与持久化边界管理，无同名字段 |
| AgentLoop 接入 | `_process_message()` / `_build_turn()` |
| 保存历史 | `_persist_turn()` / `finish_turn()` |

需要明确区分：

- **V2 实际实现：** 默认 System Prompt、根目录 AGENTS.md、简化 Token 估算、按完整用户轮次裁剪历史、ToolCall/ToolResult 配对校验、`ContextBudgetExceeded`、`/context`。单 CLI Channel、单默认 Session、JSON 持久化、原子替换、多轮历史恢复保持不变。
- **Nanobot 正式源码的高级能力：** 多 Session、Session Queue、Session Lock、更完整的并发/一致性控制、SQLite/数据库存储、完整 ContextBuilder（SOUL.md/USER.md/Memory/Skills）、ContextGovernor 压缩、Memory、Hooks、Checkpoint、MCP、多通道等。
- **V2 不支持：** 多 Session 切换、会话分叉/过期、跨进程并发写入保证、SQLite、自动历史摘要/模型压缩、Memory、Hooks、Checkpoint、MCP、多 Channel、运行中消息注入。

本实现不声称与 Nanobot 在 Session 并发、一致性、上下文压缩或恢复能力上完全等价。详细的源码对照见 [`docs/context_engineering.md`](docs/context_engineering.md)。

## 项目结构

```text
mini-harness/
├── main.py                       # 兼容入口：交互式 / --demo / --list-providers
├── pyproject.toml
├── uv.lock
├── README.md
├── docs/
│   └── context_engineering.md    # V2 上下文工程源码学习文档
├── mini_harness/
│   ├── __init__.py
│   ├── __main__.py
│   ├── workspace.py              # Workspace / WorkspaceResolver / workspace_id
│   ├── bus/
│   │   ├── events.py
│   │   └── queue.py
│   ├── agent/
│   │   ├── loop.py               # Session 感知的 MessageBus 消费者
│   │   ├── context.py            # V2 ContextBuilder / ContextConfig / Token 估算
│   │   ├── prompt.py             # V1 兼容导出，实际模板在 prompts/defaults.py
│   │   └── runner.py
│   ├── prompts/
│   │   ├── __init__.py
│   │   └── defaults.py           # 默认 System Prompt 的唯一来源
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
│       ├── edit.py               # EditFileTool：精确字符串替换
│       ├── filesystem.py         # GrepTool / ListDirTool / ReadFileTool / WriteFileTool
│       └── registry.py
└── tests/
    ├── test_bus.py
    ├── test_runner.py
    ├── test_prompt.py
    ├── test_context.py
    ├── test_context_budget.py
    ├── test_context_integration.py
    ├── test_e2e.py
    ├── test_providers.py
    ├── test_cli.py
    ├── test_workspace.py
    ├── test_session.py
    ├── test_session_integration.py
    ├── test_streaming.py
    └── test_filesystem_tools.py
```

## 测试

```bash
uv run pytest -q
```

当前结果：

```text
163 passed
```

测试覆盖：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_bus.py` | 入站/出站消息往返、asyncio.Queue FIFO |
| `tests/test_prompt.py` | System Prompt 的 Workspace 路径和关键规则（V1 兼容入口） |
| `tests/test_context.py` | 默认 System Prompt、AGENTS.md 加载/缺失/更新/空文件/大小限制/UTF-8/cwd 隔离、消息顺序与 history 不可变性、Token 估算 |
| `tests/test_context_budget.py` | 预算公式、Tool Schema 预留、长历史按轮次裁剪、ToolCall/ToolResult 配对、孤立 tool 丢弃、超预算异常、统计字段 |
| `tests/test_context_integration.py` | Session 完整历史保留、System Prompt 不落盘、AGENTS.md 每轮刷新、跨重启工具调用 ID、超预算保护、Runner 新增消息边界、`/context` |
| `tests/test_runner.py` | MockProvider、消息历史、tool_call_id 配对、max_iterations、预算耗尽收尾、System Prompt 隔离、参数校验、工具异常 |
| `tests/test_e2e.py` | V0 完整 E2E 调用链、CLI 演示输出、Loop 正常退出与停止时序 |
| `tests/test_providers.py` | 预设完整性、工厂构建、环境变量、OpenAI/Anthropic 请求响应转换、HTTP 错误 |
| `tests/test_cli.py` / `tests/test_reasoning_settings.py` / `tests/test_output_tokens_config.py` | Profile 存储与权限、默认配置、输出额度校验与协议映射、多轮对话、模型切换、推理菜单保存/恢复/取消、实际请求参数更新与历史保留 |
| `tests/test_workspace.py` | 稳定 workspace_id、不同路径隔离、`../`、绝对路径和符号链接越界检查 |
| `tests/test_session.py` | 缺失/损坏 JSON、Workspace 冲突、独立副本、清空、原子保存失败、tool_calls 持久化、单实例锁 |
| `tests/test_session_integration.py` | Session 重启恢复、不同 Workspace 隔离、历史不重复、第二轮 Provider 入参精确包含 system/user/assistant/tool_calls/tool_result、持久化失败、预算耗尽收尾落盘、失败回合不落盘、CLI 命令 |
| `tests/test_streaming.py` | Mock/OpenAI/Anthropic 的流式分片、tool_calls 聚合、Runner 不写中间 chunk、CLI 不重复打印最终回答 |
| `tests/test_filesystem_tools.py` | `list_dir`/`grep`/`read_file`/`write_file`/`edit_file` 的递归发现、噪声目录过滤、正则搜索、路径校验、分页、UTF-8/二进制错误、Markdown 创建与覆盖、精确替换、CLI 工具注册 |

## V2 已实现功能与限制

### 已实现

- [x] Workspace 解析、固定、稳定 ID 和 Session 存储映射
- [x] 单 Channel、单默认 Session 的加载、保存与清空
- [x] JSON + `schema_version`、UTF-8、`ensure_ascii=False`
- [x] 同目录临时文件 + `flush` + `fsync` + `os.replace` 的原子保存
- [x] JSON 损坏、Workspace 不匹配、保存失败时明确报错并保留旧文件
- [x] AgentLoop 接入 Session，并用 `AgentRunResult.new_messages` 显式边界追加历史
- [x] CLI 启动摘要、`/history`、`/context`、`/last-prompt`、`/clear`、`/exit`
- [x] CLI 单实例 `flock` 建议锁
- [x] `list_dir` / `grep` / `read_file` / `write_file` / `edit_file` 文本工具，使用 Workspace 路径策略
- [x] 独立 ContextBuilder：默认 System Prompt + 根目录 AGENTS.md + 历史选择 + Token 预算
- [x] 按完整用户轮次保留最近历史，ToolCall/ToolResult 严格配对
- [x] `ContextBudgetExceeded`：超预算不截断当前输入、不改 Session、不调用 Provider
- [x] `/context` 展示真实 System Prompt 状态、AGENTS.md、Session/选择/丢弃消息数和估算 Token
- [x] ContextGovernor：每次 Provider 请求前检查预算，软/硬阈值触发 Compact
- [x] 最近完成轮次摘要保护、旧历史结构化摘要、当前用户/工具链 Delta 保护
- [x] 大型 Tool Result Provider 侧截断（带 truncated 标记与关键元数据），Session 原始结果不变
- [x] `/last-prompt` 展示 iteration、估算 Token、Compact 状态、工具消息数与真实 messages
- [x] 默认 32 轮 Agent 预算、预算耗尽收尾
- [x] OpenAI-compatible / Anthropic SSE 流式文本输出，`--no-stream` 可关闭
- [x] 统一 `max_output_tokens` 配置，区分能力上限与单次请求额度
- [x] 失败回合不写入 Session
- [x] 持久化 Session Summary Checkpoint（原始历史 + 归档边界 + 摘要 + 未归档消息）
- [x] `/compact` 手动压缩；Idle Compaction 默认 15 分钟 / 60 秒检查、可配置为 0 关闭
- [x] Memory Archive：`memory/MEMORY.md`、`history.jsonl`（cursor 单调、checkpoint 幂等）、`.dream_cursor`
- [x] Dream：默认每 2 小时检查、单次最多 20 条，结构化 add/update/remove、校验、去重、原子写、失败不推进 cursor
- [x] `/dream` 手动整理长期记忆，与定时 Dream 互斥
- [x] V0/V1 原有测试与上下文、命令执行、文件工具和开发闭环回归测试

### 限制

- Workspace 重命名/移动会产生新 `workspace_id`，不自动迁移旧 Session。
- 只有一个 CLI Channel、一个默认 Session，不支持多 Session 切换。
- JSON 持久化是单进程单写者方案；`flock` 只保护 CLI，不保护任意库调用。
- Context Token 是估算值，不是模型 Tokenizer 的精确计数；8K/1K 只是教学默认值，必须按实际模型配置。
- AGENTS.md 只读取 Workspace 根目录的一份；不递归、不合并、不解析优先级；超过 16 KiB 整份跳过。
- Compact 只做单次轻量摘要 + 确定性回退，不做分块递归摘要、向量检索或分层记忆。
- 长期记忆仅支持 Workspace 内 `memory/` 文件，不支持多 Agent 共享记忆、向量检索或 Skills 自动创建；Dream 仍依赖当前 Provider，没有独立离线模型。
- 没有 Hooks、通用 Checkpoint、MCP、多通道和运行中消息注入。
- 文本读取仍只支持 UTF-8，不解析图片、PDF 或 Office；本地 shell 命令通过独立 `run_command` 提供。
- 路径检查不是完整安全沙箱，符号链接竞态和并发文件系统修改是后续强化项。
- 没有 SQLite；JSON 存储不适合大规模或高并发使用。

## 后续建议（V3，不在本次实现）

1. **Session Queue / 并发控制**：每个 Session 串行处理回合，为多客户端共享同一 Session 做准备。
2. **SQLite SessionStore**：替换 JSON 文件，引入事务、索引和更可靠的恢复能力。
3. **更完整的文件工具**：在现有 `list_dir`/`grep`/`read_file`/`write_file`/`edit_file` 上增加图片/PDF/Office 读取和符号链接竞态防护。
4. **更高级 Context Governance**：分块递归摘要、跨回合摘要缓存、ContextGovernor 指标与可观测性面板。
5. **Memory / Skills / Hooks / MCP**：在现有 Dream/Memory 基础上增加 Skills 自动沉淀与跨 Workspace 记忆。

## 安全提示

- API Key 以明文保存在本机 `~/.mini-harness/config.json`，文件权限为 `0600`；不要提交到版本库或共享给他人。
- Session JSON 不保存 API Key，但仍可能包含用户对话和工具输出，请按需保护本地数据目录。
- 不要把 `Workspace.resolve_path()` 当成完整沙箱；生产环境需要更严格的路径和文件描述符级防护。

## 参考

- [HKUDS/nanobot](https://github.com/HKUDS/nanobot)
- [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat)
- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages)
- Python `asyncio` 文档：https://docs.python.org/3/library/asyncio.html
