# Mini Harness

Mini Harness 是一个基于 Python `asyncio` 构建的最小 Agent Harness，用于演示一个 LLM Agent 如何完成完整的工具调用闭环：

```text
接收消息 → 调用模型 → 执行工具 → 再次调用模型 → 返回结果
```

项目参考 [Nanobot](https://github.com/HKUDS/nanobot) 的分层设计，但有意保持最小实现：不依赖 Agent 框架、不接入真实 LLM、不使用数据库。当前版本为 **V0**，重点验证 Agent Loop、Provider、ToolRegistry 与消息总线之间的协作关系。

## 当前状态

- 已实现可运行、可测试的 V0 工具调用闭环。
- 内置离线 `MockProvider`，第一次请求 `add(a=2, b=3)`，第二次根据工具结果生成最终回答。
- 运行 `uv run python main.py` 输出 `结果是 5`。
- 当前仅支持 CLI、单消费者、顺序处理和 `add` 示例工具。

## 主要特性

- **异步消息总线**：基于 `asyncio.Queue` 解耦 CLI 与 Agent 核心，入站/出站消息独立排队。
- **清晰的 Agent 分层**：`AgentLoop` 负责调度，`AgentRunner` 负责模型与工具迭代，职责互不混杂。
- **可替换的 Provider**：通过 `LLMProvider` 抽象接入模型；V0 使用确定性的 `MockProvider`。
- **安全的工具调用**：工具注册、JSON Schema 参数校验、异常兜底和 `tool_call_id` 配对。
- **完整消息历史**：记录 user、assistant tool calls、tool results 和最终 assistant 回答。
- **可控的迭代上限**：`max_iterations` 防止工具调用无限循环。
- **零运行时依赖**：仅使用 Python 标准库；测试使用 pytest。

## 架构

```text
CLI
 ⇅  InboundMessage / OutboundMessage
MessageBus
 ⇅
AgentLoop
 ⇅
AgentRunner  ⇄  LLMProvider（V0：MockProvider）
 ⇅
ToolRegistry
 ⇅
AddTool
```

### 一次工具调用的执行流程

1. CLI 创建 `InboundMessage` 并发布到 `MessageBus`。
2. `AgentLoop` 消费入站消息，构造 `AgentRunSpec` 并交给 `AgentRunner`。
3. `AgentRunner` 调用 `MockProvider.chat(messages, tools)`。
4. MockProvider 返回 `add(a=2, b=3)` 的工具调用请求。
5. `AgentRunner` 追加 assistant tool call 消息，调用 `ToolRegistry.execute()`，得到 `ToolResult("5")`。
6. Runner 追加带 `tool_call_id` 的 tool 结果消息，并再次调用 MockProvider。
7. MockProvider 根据 tool 结果返回 `"结果是 5"`，Runner 生成 `AgentRunResult`。
8. `AgentLoop` 包装成 `OutboundMessage` 发回 MessageBus，CLI 读取并打印。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

已使用 Python 3.14.5 和 uv 0.11.24 验证运行。

## 快速开始

```bash
cd mini-harness
uv sync
uv run python main.py
```

预期输出：

```text
结果是 5
```

首次执行 `uv sync` 会创建虚拟环境并安装测试依赖。

## 项目结构

```text
mini-harness/
├── main.py                       # CLI 演示入口
├── pyproject.toml                # 项目与 pytest 配置
├── uv.lock
├── README.md
├── mini_harness/
│   ├── __init__.py
│   ├── bus/
│   │   ├── events.py             # InboundMessage / OutboundMessage
│   │   └── queue.py              # MessageBus
│   ├── agent/
│   │   ├── loop.py               # AgentLoop
│   │   └── runner.py             # AgentRunner / AgentRunSpec / AgentRunResult
│   ├── providers/
│   │   ├── base.py               # Provider 抽象与响应数据结构
│   │   └── mock.py               # MockProvider
│   └── tools/
│       ├── base.py               # Tool / ToolResult / 参数校验
│       └── registry.py           # ToolRegistry + AddTool
└── tests/
    ├── test_bus.py
    ├── test_runner.py
    └── test_e2e.py
```

## 核心模块

| 模块 | 说明 |
|---|---|
| `bus/events.py` | 定义入站和出站消息数据结构 |
| `bus/queue.py` | 使用两个 `asyncio.Queue` 实现异步消息总线 |
| `agent/loop.py` | 单消费者循环，负责消费消息、调用 Runner、发布结果 |
| `agent/runner.py` | 核心 Agent 循环，维护消息历史并执行工具调用 |
| `providers/base.py` | `LLMProvider`、`LLMResponse`、`ToolCallRequest` |
| `providers/mock.py` | 按预设序列返回响应，支持根据工具结果生成最终回答 |
| `tools/base.py` | 工具抽象、工具结果和参数 Schema 校验 |
| `tools/registry.py` | 工具注册、查找、执行与异常处理，内置 `AddTool` |
| `main.py` | 组装依赖并演示完整 CLI 调用流程 |

### 消息约定

- `assistant.tool_calls[].id` 与 `tool.tool_call_id` 一一对应。
- 每个工具调用都会产生一条 `role=tool` 消息，即使工具执行失败也是如此。
- 工具失败会作为错误消息回传给模型，不会直接中断 Agent。
- 达到 `max_iterations` 时返回 `stop_reason="max_iterations"`。

## 测试

```bash
uv run pytest -q
```

预期结果：

```text
13 passed
```

测试覆盖：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_bus.py` | 入站/出站消息往返、asyncio.Queue FIFO |
| `tests/test_runner.py` | MockProvider、消息历史、tool_call_id 配对、max_iterations、参数校验、工具异常 |
| `tests/test_e2e.py` | 完整 E2E 调用链、CLI 输出、Loop 正常退出与停止时序 |

## 待做功能

V0 只覆盖最小工具调用闭环，以下能力尚未实现：

- [ ] **Session 管理**：会话隔离、历史记录、Session Queue 与 Session Lock。
- [ ] **Context Engineering**：ContextBuilder、Token 预算、上下文裁剪与压缩。
- [ ] **Memory Engineering**：长期记忆、检索、写入与 Consolidation。
- [ ] **Hooks**：Agent 回合与工具执行的生命周期钩子。
- [ ] **Checkpoint**：运行状态保存、中断恢复和任务重放。
- [ ] **MCP**：接入 MCP Server，发现并调用外部工具。
- [ ] **真实 LLM Provider**：接入 OpenAI-compatible 等真实模型 API。
- [ ] **流式响应**：支持 token 流式输出、超时、重试和错误处理。
- [ ] **多轮 CLI**：交互式输入、会话内连续对话。
- [ ] **并发与运行中注入**：多消费者、Session 并发控制、运行中追加用户消息。
- [ ] **多通道支持**：Web、Slack、Telegram 等消息通道适配器。
- [ ] **更多内置工具**：文件读写、Shell、搜索等常用工具。

## 参考

- [HKUDS/nanobot](https://github.com/HKUDS/nanobot)
- Python `asyncio` 文档：https://docs.python.org/3/library/asyncio.html
