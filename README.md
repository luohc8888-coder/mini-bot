# Mini Harness V0

一个从零实现的**最小、可运行、可测试** Agent Harness。它只实现 V0 的工具调用闭环：

```text
CLI
 → InboundMessage
 → MessageBus
 → AgentLoop
 → AgentRunner
 → MockProvider
 → ToolRegistry
 → ToolResult
 → AgentRunner（再次调用 MockProvider）
 → AgentRunResult
 → OutboundMessage
 → MessageBus
 → CLI
```

输入 `计算 2 + 3`，MockProvider 第一次请求 `add(a=2, b=3)`，Runner 执行工具得到 `5`，把结果写回消息历史并再次调用 MockProvider；第二次 Provider 读取 tool 消息，返回最终答案。CLI 输出：

```text
结果是 5
```

> V0 刻意保持最小：不实现 Session、Context Engineering、Memory、Hooks、Checkpoint、MCP，也不接入真实 LLM、数据库或 Agent 框架。

---

## 1. 运行环境

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- pytest（仅测试依赖）

本项目已在 Python 3.14.5 + uv 0.11.24 下实际运行通过。

---

## 2. 项目结构

```text
mini-harness/
├── .gitignore                    # 忽略虚拟环境、缓存与字节码
├── main.py                       # CLI 演示入口
├── pyproject.toml                # 项目与 pytest 配置
├── README.md
├── uv.lock                       # 锁定测试依赖
├── mini_harness/
│   ├── __init__.py               # 包公开接口
│   ├── bus/
│   │   ├── __init__.py
│   │   ├── events.py             # InboundMessage / OutboundMessage
│   │   └── queue.py              # MessageBus（两个 asyncio.Queue）
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── loop.py               # AgentLoop：消费消息、调度 Runner
│   │   └── runner.py             # AgentRunner：模型/工具循环
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py               # ToolCallRequest / LLMResponse / LLMProvider
│   │   └── mock.py               # MockProvider（离线、确定性）
│   └── tools/
│       ├── __init__.py
│       ├── base.py               # Tool / ToolResult / 参数 Schema 校验
│       └── registry.py           # ToolRegistry + AddTool
└── tests/
    ├── test_bus.py               # 入站/出站/FIFO 测试
    ├── test_runner.py            # Provider/Runner/工具校验与错误测试
    └── test_e2e.py               # 完整链路 + CLI 输出测试
```

后续版本会逐步增加 `session/`、`context/`、`memory/`、`hooks/` 等模块；V0 不创建这些目录，避免产生空抽象。

---

## 3. 模块职责

| 模块 | 职责 | 不负责 |
|---|---|---|
| `bus/events.py` | 定义 `InboundMessage`、`OutboundMessage` 数据对象 | Session、消息持久化 |
| `bus/queue.py` | 用两个 `asyncio.Queue` 实现发布/消费，FIFO | 调用 LLM、业务判断 |
| `agent/loop.py` | 单消费者循环；把入站消息交给 Runner；发布出站消息 | 工具执行、Provider 细节 |
| `agent/runner.py` | 核心迭代：Provider → 工具 → Provider；维护 messages | CLI、Session、Checkpoint |
| `providers/base.py` | `ToolCallRequest`、`LLMResponse`、`LLMProvider` 抽象 | 网络请求、业务逻辑 |
| `providers/mock.py` | 按预设序列返回响应；可根据 tool 结果生成最终回答 | 真实 API、数据库 |
| `tools/base.py` | `Tool` 抽象、`ToolResult`、JSON Schema 参数校验 | 工具注册、调用调度 |
| `tools/registry.py` | 注册/查找/导出定义/校验/执行工具；`AddTool` | 权限、MCP、并发执行 |
| `main.py` | 组装依赖、启动 Loop、投递输入、打印输出 | 参与 Agent 内部判断 |

核心职责边界：**业务判断不在 `AgentRunner` 中**。Runner 不知道“应该调用 add”或“答案是 5”；它只执行 Provider 给出的工具调用，并把工具结果回传。工具选择与最终回答由 `MockProvider` 的预设策略模拟。

---

## 4. 真实代码调用链

以下是从 `uv run python main.py` 到 `结果是 5` 的实际调用链（函数名与代码一致）：

```text
asyncio.run(main())
└─ main()
   ├─ MessageBus()
   ├─ ToolRegistry().register(AddTool())
   ├─ build_demo_provider()
   │  └─ MockProvider([
   │       LLMResponse(tool_calls=[ToolCallRequest(id="call_add_1",
   │                                                name="add",
   │                                                arguments={"a": 2, "b": 3})]),
   │       MockProvider.final_from_tool_result("结果是")
   │     ])
   ├─ AgentRunner(provider, tools)
   ├─ AgentLoop(bus, runner, max_iterations=8)
   ├─ asyncio.create_task(agent_loop.run())      # 常驻 Loop 在后台等待消息
   ├─ await bus.publish_inbound(InboundMessage("计算 2 + 3"))
   │  └─ await asyncio.Queue.put(...)
   │
   ├─ AgentLoop.run()
   │  ├─ await asyncio.timeout(0.1): await bus.consume_inbound()
   │  ├─ await _dispatch_inbound(message)
   │  │  └─ await _process_message(message)
   │  │     ├─ AgentRunSpec(initial_messages=[{"role": "user", ...}],
   │  │     │                max_iterations=8)
   │  │     └─ await AgentRunner.run(spec)
   │  │        ├─ messages = deepcopy(spec.initial_messages)
   │  │        └─ await AgentRunner._run_core(spec, messages)
   │  │           ├─ tools.get_definitions()
   │  │           ├─ await provider.chat(messages, tools)
   │  │           │  └─ MockProvider 返回 add 工具调用
   │  │           ├─ messages.append(assistant + tool_calls)
   │  │           ├─ await tools.execute("add", {"a": 2, "b": 3})
   │  │           │  ├─ ToolRegistry 校验参数
   │  │           │  └─ AddTool.execute(2, 3) → ToolResult("5")
   │  │           ├─ messages.append({"role": "tool",
   │  │           │                     "tool_call_id": "call_add_1",
   │  │           │                     "name": "add",
   │  │           │                     "content": "5"})
   │  │           ├─ continue                    # 进入下一轮模型调用
   │  │           ├─ await provider.chat(messages, tools)
   │  │           │  └─ final_from_tool_result 读取 tool 消息 → "结果是 5"
   │  │           ├─ messages.append(assistant final)
   │  │           ├─ break                       # 得到最终回答，结束循环
   │  │           └─ return AgentRunResult(
   │  │                final_content="结果是 5",
   │  │                messages=[user, assistant, tool, assistant],
   │  │                tools_used=["add"],
   │  │                stop_reason="completed")
   │  │     └─ OutboundMessage("结果是 5")
   │  └─ await bus.publish_outbound(outbound)
   │
   ├─ await asyncio.wait_for(bus.consume_outbound(), 5.0)
   ├─ print(outbound.content)                      # 结果是 5
   └─ finally: agent_loop.stop(); await loop_task   # 正常回收常驻任务
```

### 4.1 messages 的真实变化

第一次调用 Provider 前：

```python
[
    {"role": "user", "content": "计算 2 + 3"},
]
```

工具调用轮结束后：

```python
[
    {"role": "user", "content": "计算 2 + 3"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_add_1",
                "name": "add",
                "arguments": {"a": 2, "b": 3},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_add_1",
        "name": "add",
        "content": "5",
    },
]
```

第二次 Provider 返回最终回答后：

```python
[
    # ...前三条同上...
    {"role": "assistant", "content": "结果是 5"},
]
```

关键点：

- `assistant.tool_calls[].id` 与 `tool.tool_call_id` 严格一致。
- 每个工具调用都有一条 `role=tool` 结果，即使工具失败也如此。
- `continue` 用于进入下一轮模型调用；`break` 用于在得到最终回答后退出。
- `max_iterations` 是模型调用轮数上限；达到上限时 `stop_reason="max_iterations"`、`final_content=None`。

---

## 5. 关键实现说明

### 5.1 为什么使用 asyncio

- `MessageBus` 的发布/消费都基于 `asyncio.Queue`。`publish_inbound` 和
  `consume_inbound` 都是 `async`：生产者和消费者通过 `await` 挂起/恢复，
  而不是忙等。
- `AgentRunner` 中每一次 `await provider.chat(...)` 都可能代表真实网络 I/O；
  每一次 `await tools.execute(...)` 都可能代表文件、命令或远程工具。V0
  即使使用 Mock/加法工具，也保留这些 `await` 边界，以便后续无痛替换实现。
- `main()` 用 `asyncio.create_task(agent_loop.run())` 启动常驻消费者，这样
  主协程可以继续发布入站消息并等待出站结果。
- `AgentLoop.run()` 用 `asyncio.timeout(0.1)` 包装阻塞的
  `consume_inbound()`，每 0.1 秒检查一次 `_running`。`stop()` 后最多一个
  周期即可退出，不会留下永久挂起的消费者任务。`main()` 在 `finally` 中
  `await loop_task`，完成确定性回收。

### 5.2 为什么业务不写死在 Runner 中

Runner 只认识两个接口：

```python
response = await provider.chat(messages, tools)
result = await tools.execute(tool_call.name, tool_call.arguments)
```

它不知道“用户是在做加法”，也不知道“应该调用哪个工具”。替换
`MockProvider` 或工具注册表即可改变行为，Runner 本身无需修改。

### 5.3 工具失败处理

`ToolRegistry.execute()` 对三类问题统一返回 `ToolResult.error(...)`：

1. 未知工具名；
2. 参数缺失、类型错误或包含未声明参数；
3. 工具内部抛出异常。

Runner 会把错误文本作为 `role=tool` 消息回传给模型，然后继续下一轮，
而不是让整个进程崩溃。这保证了“工具调用与工具结果保持配对”。

### 5.4 迭代限制

`for iteration in range(spec.max_iterations)` 最多允许指定轮次的 Provider
调用。如果循环因工具调用不断 `continue`，最终由 `for ... else` 分支记录
`stop_reason="max_iterations"`；不会出现无限循环。

---

## 6. 运行命令

在 `mini-harness/` 目录下执行：

```bash
uv sync
uv run python main.py
```

预期输出：

```text
结果是 5
```

首次运行会创建 `.venv/` 并安装 pytest/pytest-asyncio 开发依赖。

---

## 7. 测试命令

```bash
uv run pytest -q
```

已验证结果：

```text
.............                                                            [100%]
13 passed
```

测试覆盖：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_bus.py` | 入站消息往返、出站消息往返、入站/出站 `asyncio.Queue` FIFO |
| `tests/test_runner.py` | MockProvider 工具调用、Runner messages 变化、单个/多个 tool_call_id 配对、max_iterations、参数校验、未知工具、工具异常不崩溃 |
| `tests/test_e2e.py` | `InboundMessage → AgentLoop → AgentRunner → MockProvider → add → MockProvider → OutboundMessage` 完整链路；CLI 实际打印；Loop 正常退出与启动前 stop 时序 |

E2E 测试使用真实组件协作并通过 `asyncio.Queue` 传递消息，不是伪造的
返回值；第二次 Provider 调用读取的是 Runner 实际追加的 tool 消息。

---

## 8. 与 Nanobot 的对应关系

参考项目：[HKUDS/nanobot](https://github.com/HKUDS/nanobot)。本项目按
Nanobot 的分层方式抽取出一个最小可运行版本，结构对应如下：

| V0 模块 | Nanobot 对应源码 | V0 保留 | V0 未实现 |
|---|---|---|---|
| `bus/events.py` | [`nanobot/bus/events.py`](https://github.com/HKUDS/nanobot/blob/main/nanobot/bus/events.py) | Inbound/Outbound 消息基本形状 | channel/chat_id、媒体、event、delivery、复杂元数据 |
| `bus/queue.py` | [`nanobot/bus/queue.py`](https://github.com/HKUDS/nanobot/blob/main/nanobot/bus/queue.py) | `asyncio.Queue` 双向总线、发布/消费 | admission、事件订阅、outbound recorder、drain |
| `agent/loop.py` | [`nanobot/agent/loop.py`](https://github.com/HKUDS/nanobot/blob/main/nanobot/agent/loop.py) | 单消费者 `run`、`_dispatch_inbound`、`_process_message` | Session Queue/Lock、并发 turn、命令、后台任务、崩溃恢复 |
| `agent/runner.py` | [`nanobot/agent/runner.py`](https://github.com/HKUDS/nanobot/blob/main/nanobot/agent/runner.py) | `AgentRunSpec`、`AgentRunResult`、`run`、`_run_core`、工具循环 | Context 治理、压缩、重试、hooks、checkpoint、注入、流式输出 |
| `providers/base.py` | [`nanobot/providers/base.py`](https://github.com/HKUDS/nanobot/blob/main/nanobot/providers/base.py) | `ToolCallRequest`、`LLMResponse`、`LLMProvider` | 多 Provider、tool 参数修复、usage、reasoning、streaming |
| `providers/mock.py` | 无对应生产实现（学习用替代） | 离线脚本化响应 | 真网络请求 |
| `tools/base.py` | [`nanobot/agent/tools/base.py`](https://github.com/HKUDS/nanobot/blob/main/nanobot/agent/tools/base.py) | `Tool`、`ToolResult`、参数校验思路 | Schema 类体系、context-aware、复杂类型转换 |
| `tools/registry.py` | [`nanobot/agent/tools/registry.py`](https://github.com/HKUDS/nanobot/blob/main/nanobot/agent/tools/registry.py) | 注册、查找、定义导出、校验、执行、异常兜底 | MCP、权限、并发工具、结果裁剪、工具事件 |

Nanobot 当前代码比 V0 大很多。对应关系是**分层与职责**的对应，不是逐行复制；
V0 只保留理解 Agent Harness 主流程所需的最小集合。

---

## 9. V0 尚未实现的功能

以下能力全部**有意未实现**，不会提前引入：

- **Session**：没有 `Session`、`SessionManager`、会话历史持久化、Session Queue/Lock。
- **Context Engineering**：没有 `ContextBuilder`、上下文裁剪、Token 估算、上下文压缩。
- **Memory Engineering**：没有长期记忆、检索或 Consolidation。
- **Hooks**：没有 `before/after` 生命周期 hook。
- **Checkpoint**：没有运行中断点、恢复、重放。
- **MCP**：没有 MCP 客户端/服务端、外部工具发现。
- **真实 LLM**：不接 API，不处理鉴权、流式响应、限流、重试。
- **并发与运行中注入**：单消费者、顺序处理，不支持运行中追加用户消息。
- **多通道**：只有 CLI 演示，没有 Slack/Telegram/Web 等适配器。
- **额外持久化**：无数据库、无文件型会话存储。

这些能力将按 V1～V7 逐步添加；V0 的验收标准就是 `uv sync`、
`uv run python main.py`（输出 `结果是 5`）与 `uv run pytest`（全部通过）。
