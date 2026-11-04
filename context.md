# Mini Harness 上下文工程（Context Engineering）实现分析

> 版本：Mini Harness V2
> 分析范围：`mini_harness/agent/context.py`、`prompts/defaults.py`、`agent/loop.py`、`agent/runner.py`、`cli/app.py`

## 1. 总体设计：三块职责严格分离

V2 上下文工程的核心是「谁负责什么」的边界设计：

```text
SessionManager / Session   →  只持久化完整历史（不改、不裁剪）
ContextBuilder             →  只把历史构建成一次请求的 request_messages
AgentRunner                →  只执行 模型→工具→模型 迭代循环
```

- `ContextBuilder` 不读写 Session 文件、不调用 Provider、不执行工具，**唯一外部 I/O 是读取 Workspace 根目录的 `AGENTS.md`**。
- `history` 只被内存读取和 `deepcopy`，**永不修改**调用方数据。
- System Prompt 只存在于临时 `request_messages` 中，**绝不写入 Session**。

这正是 README 强调的：「Session 保存历史、ContextBuilder 构建模型输入、AgentRunner 执行，三者不混合职责」。

## 2. 请求上下文的组装流程

`ContextBuilder.build_context(history, current_message)` 是主入口，产出一个 `BuiltContext(messages, stats)`：

```text
final request_messages =
    [ system_prompt ]              # 默认提示词 + 可选 AGENTS.md
  + [ *selected_history ]          # 按预算裁剪后的历史（连续后缀）
  + [ current_user_message ]       # 当前用户输入（永不裁剪）
```

关键函数：

- `build_system_prompt()` → 组装 System Prompt
- `_select_history()` → 选择历史
- `build_messages()` → 仅返回 messages（`build_context().messages` 的便捷封装）
- `inspect(history)` → 无当前输入，仅供 CLI `/context` 调试统计

## 3. System Prompt 的组装

### 3.1 唯一模板来源：`prompts/defaults.py`

- `DEFAULT_SYSTEM_PROMPT` 是**默认提示词的唯一来源**，AgentLoop / ContextBuilder / AgentRunner 都不得各自拼接。
- 唯一模板变量是 `{workspace}`，使用 `str.replace` 而不是 `str.format`（因为 Workspace 路径可能含特殊字符，避免 format 的花括号问题）。
- 内容包含：工作目录、核心规则（不得编造工具结果、如实报告失败、遵守 Workspace 边界、保持简洁）、工具使用规则（先 list_dir、优先 README/入口、用 grep 定位、读 3~8 个文件就写报告等）。
- `build_default_system_prompt()` 是纯函数：不读文件、不访问 Session、不缓存。

### 3.2 AGENTS.md 自动加载（`_read_agents_md` / `_build_system_prompt_with_status`）

- 只读取 `WorkspaceResolver` 固定下来的 **Workspace 根目录** 的 `AGENTS.md`，不递归、不合并多份。
- 以独立段落 `# Workspace Instructions (AGENTS.md)` 追加到默认提示词之后，并声明「若与通用规则冲突以 AGENTS.md 为准（安全边界和『不得编造工具结果』除外）」。
- **每轮重新读取**，文件修改后下一轮立即生效；不改文件、不写入 Session。
- 失败降级为默认提示词，不打断对话。状态机 `status` 取值：
  - `loaded` / `missing` / `empty` / `disabled` / `too_large` / `decode_error` / `read_error` / `not_file`
- 规则细节：
  - UTF-8 读取；空文件忽略；
  - 大小上限 `agents_md_max_bytes`（默认 16 KiB），**超过整份跳过，不截断半份**；
  - 读取前后各做一次大小检查（防读取期间被改写）。

## 4. Token 估算：保守近似

V2 不绑定具体模型的 Tokenizer，使用简化估算（`estimate_tokens`）：

- **ASCII 字符**约 4 字符 / Token（向上取整）；
- **非 ASCII 字符**（中文、emoji 等）约 1 字符 / Token；
- 空文本返回 0，非空至少 1。

`estimate_message_tokens` 在 content 之外还计入结构化开销：

| 开销项 | 常量 | 说明 |
|---|---|---|
| 每条消息结构开销 | `MESSAGE_OVERHEAD_TOKENS = 4` | role、分隔符等 |
| tool_calls 开销 | `TOOL_CALL_OVERHEAD_TOKENS = 8` | 工具调用结构 |
| tool 定义开销 | `TOOL_DEFINITION_OVERHEAD_TOKENS = 8` | Schema 整体 |

- 还累计 role、`tool_call_id`、`name`、序列化后的 tool_calls JSON 等。
- `estimate_tool_definitions_tokens`：Tool Schema 不在 messages 里，但真实模型会计入窗口，所以预算要预留。
- 明确文档化：**估算 Token ≠ 模型真实 Token**，`reserve_output_tokens` 同时充当估算误差的安全余量。

## 5. 上下文预算计算

`ContextConfig`（可配置，默认值）：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `max_context_tokens` | 8192 | 模型上下文窗口估算总量（教学值） |
| `reserve_output_tokens` | 1024 | 输出预留 + 估算误差安全余量 |
| `include_agents_md` | True | 是否加载 AGENTS.md |
| `agents_md_max_bytes` | 16 * 1024 | AGENTS.md 大小上限 |
| `safety_margin_tokens` | 0 | 额外历史安全余量（默认 0，因 reserve 已承担缓冲） |

预算公式（`build_context`）：

```text
available_input      = max_context_tokens - reserve_output_tokens          # 默认 7168
fixed_actual         = system_prompt_tokens + current_message_tokens + tool_schema_tokens
fixed_with_margin    = fixed_actual + safety_margin_tokens

if fixed_with_margin > available_input:
    → check_budget=True 时抛 ContextBudgetExceeded
    → check_budget=False 时 history_budget = 0（供 /context 展示超预算状态）
else:
    history_budget = available_input - fixed_with_margin
```

**关键保护**：当 System Prompt + 当前消息 + 必要开销已超预算时：
- 抛出 `ContextBudgetExceeded`（携带 `estimated_tokens` / `available_input_tokens` / `breakdown` 明细）；
- AgentLoop 返回 `context_budget_exceeded` 错误；
- **不截断当前输入、不清空历史、不调用 Provider**。

`ContextConfig.__post_init__` 做参数校验：`max_context_tokens > 0`、`reserve_output_tokens` 非负且 `< max_context_tokens` 等。

## 6. 历史选择：按完整用户轮次裁剪

`_select_history(history, history_budget)` 是裁剪策略的核心：

1. `_split_history_into_turns`：**以 user 消息为轮次边界**切分历史；历史开头若非 user，则作为独立片段（后续会判非法丢弃）。
2. 从**最近轮次向旧**遍历（`reversed(turns)`），累计 Token，直到预算不足。
3. 遇到 **工具结构不合法** 或 **预算不足** 的轮次**立即 break**，保证结果始终是**连续后缀**，不会在历史中间留空洞。
4. 只用 `selected_turns.insert(0, turn)` 前插，最终平铺成 `selected`。

`_is_valid_turn(turn)` 严格校验工具协议完整性：

- 轮次必须以 `user` 开始，且轮内不再出现第二个 user；
- 每个 `assistant.tool_calls` 的每个 id 必须有**唯一、紧随其后**的 `role=tool` 结果；
- tool 结果必须能对应到本轮声明的某个 tool_call，不允许孤立 tool、缺失结果、重复结果；
- 兼容简化格式与 OpenAI `function` 格式读取 id/name（`_tool_call_id` / `_tool_call_name`）。

因此最终一定会保证：**不输出孤立 tool 消息，也不输出缺失 tool result 的 assistant tool_calls**。

## 7. 显式边界：`AgentRunResult.new_messages` 防止上下文污染

这是 V2 防止「临时请求上下文」污染 Session 的关键机制。

`AgentRunner._run_core` 中：

```python
input_message_count = len(messages)   # 记录输入边界
...
new_messages = deepcopy(messages[input_message_count:])   # 只取本轮新增
```

`AgentRunResult` 暴露：
- `input_message_count`：本轮输入消息条数（显式边界，不用 `len(history)` 推断）；
- `new_messages`：本轮新增的 assistant tool_calls / tool 结果 / 最终 assistant，**不含** `initial_messages`。

`AgentLoop._process_with_session` 保存前做多重校验：

- `input_count == len(request_messages)`；
- `result.messages` 是输入历史的**完整前缀**：`result.messages[:input_count] == request_messages`；
- 尾部恰为 `new_messages`；
- **`new_messages` 中不得含 system 消息**（否则提示词污染，拒绝保存）。
- 任一校验失败 → 返回 `session_history_error`，拒绝保存。

最终写回 Session 的内容：

```python
session.messages = [
    *deepcopy(history),          # 完整旧历史（原始，未被裁剪）
    deepcopy(current_message),   # 当前用户消息（只保存一次）
    *deepcopy(new_messages),     # 本轮新增执行消息
]
```

> 注意：Session 保存的是**完整历史 + 当前消息 + 新增消息**，而不是 `request_messages`（含 System Prompt 和裁剪后的历史）。System Prompt 与裁剪丢弃的历史都不会落盘。

## 8. 未完成回合不落盘

`_process_with_session` 只在 `result.stop_reason in {"completed", "max_iterations_finalized"}` 时才保存 Session。

- `max_iterations`（未收尾）的回合不进 Session；
- `finalize_on_max_iterations=True` 时，预算耗尽会再请求一次「不带工具的总结」，成功则记为 `max_iterations_finalized` 并允许落盘。

## 9. 可观测性：`ContextStats` 与 `/context`

`ContextStats` 只含数量与估算值（不含正文/API Key/完整历史），可安全展示。字段覆盖：各分段 Token、AGENTS.md 状态、Session/选择/丢弃消息数、角色分布、`estimated_input_tokens` 等；`log_line()` 生成单行 Debug 摘要。

CLI `_print_context()`（`/context`）调用 `context_builder.inspect(session.messages)`：

- **只读 Session + 纯内存构建**，不保存 Session、不调用 LLM、不打印正文；
- 展示：Workspace、System Prompt Token、AGENTS.md 状态（loaded/missing/empty/too_large/decode_error...）、Tool Schema、Safety Margin、Session 消息数与角色分布、Selected/Dropped 消息数与角色分布、Estimated Input Tokens、Configured Input Budget、History Budget、Max Context / Reserve Output。

## 10. CLI 接线

- `main.py` 提供 `--max-context-tokens` / `--reserve-output-tokens` 参数；
- `cli/app.py` 用它们构造 `ContextConfig(max_context_tokens, reserve_output_tokens)`；
- `InteractiveCLI` 构造 `ContextBuilder(workspace, context_config, tool_definitions=...)`；
- 传给 `AgentLoop(context_builder=..., context_config=...)`；
- 未显式传入且启用 Session 时，AgentLoop 会基于 `session_manager.workspace` 和 `runner.tools.get_definitions()` **自动创建** ContextBuilder（V0 无 Session 模式则构建上下文，保持单条 user 消息直接交给 Runner）。

## 11. 已知限制（上下文工程相关）

- Token 是**估算值**，不是模型 Tokenizer 精确计数；8K/1K 是教学默认值，需按真实模型配置 `reserve_output_tokens`（应不小于 Provider 的 `max_tokens`，默认 4096）。
- 裁剪策略仅**按完整用户轮次保留最近历史**；不做自动摘要、模型压缩或分层记忆。
- AGENTS.md 只读根目录一份，不递归/不合并/不解析优先级，超 16 KiB 整份跳过。
- 无长期 Memory、Hooks、Checkpoint、MCP、多通道与运行中消息注入。

## 12. 小结

Mini Harness V2 的上下文工程可以概括为一条清晰的流水线：

```text
默认 System Prompt（defaults.py 唯一来源）
  + 根目录 AGENTS.md（每轮读取、失败降级、大小限制）
  + Token 预算（保守估算 + 输出预留 + 安全余量）
  + 按完整用户轮次裁剪历史（严格 ToolCall/ToolResult 配对）
  → request_messages（临时上下文）
  → AgentRunner（只执行循环，返回 new_messages 显式边界）
  → Session 只追加「当前消息 + 新增消息」（完整旧历史保留）
```

三个设计要点保证了正确性：**职责分离**（构建/执行/持久化互不混合）、**预算安全**（超预算即报错而绝不静默截断/清空）、**边界显式**（`new_messages` 让临时上下文永不污染持久化历史）。
