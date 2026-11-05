# Mini Bot V2 上下文压缩 / 长期记忆审查报告

> 审查时间：2026-11-05
> 审查范围：`mini_harness/session/manager.py`、`mini_harness/agent/context.py`、
> `mini_harness/agent/compaction.py`、`mini_harness/agent/runner.py`、
> `mini_harness/agent/loop.py`、`mini_harness/cli/app.py`
> 结论：现有 Token Compaction 链路完整可用；**Session Summary Checkpoint、
> Idle Compaction、`/compact`、Memory Archive、Dream 均未实现**，本次为增量补齐。

## 能力矩阵

| 能力 | 已实现/部分实现/未实现 | 对应源码 | 需要修改的内容 |
|---|---|---|---|
| Session 会话持久化、重启恢复、Workspace 隔离 | 已实现 | `mini_harness/session/manager.py`（`Session`、`SessionManager.load/save`、原子写、`lock()`）、`mini_harness/workspace.py` | 保持现有逻辑；`Session` 增加 `summary`、`summary_boundary`、`summary_checkpoint_id` 三个持久化字段并兼容旧文件 |
| ContextBuilder 组装 System Prompt、AGENTS.md、历史、当前请求、工具定义 | 已实现 | `mini_harness/agent/context.py`（`build_context`、`_build_system_prompt_with_status`、`_select_history`） | 增量加入 Long-term Memory 与 Session Summary；请求只使用 `summary_boundary` 之后的未归档历史 |
| 按模型 Context Window 与输出预留动态计算 Token Budget | 已实现 | `mini_harness/agent/context.py`（`ContextConfig`、`ContextConfig.from_provider`、`available_input_tokens`） | 新增压缩/记忆相关配置项；预算公式保持不变 |
| 优先近期完整轮次、保护 Tool Call/Tool Result 配对 | 已实现 | `mini_harness/agent/context.py`（`_split_history_into_turns`、`_is_valid_turn`、`_select_history`、`build_completed_turn_digest`） | 复用；持久化压缩额外只选择“以最终 assistant 回答收尾”的完整轮次 |
| 历史超预算时摘要或裁剪 | 已实现（仅请求内临时摘要） | `mini_harness/agent/compaction.py`（`ContextCompactor`、`_build_transcript`、`_deterministic_fit`） | 复用摘要能力；新增 `summary_text` 输出纯摘要正文供持久化 |
| AgentRunner 每次 Provider 请求前检查 Token Budget | 已实现 | `mini_harness/agent/runner.py`（`_request` → `ContextGovernor.prepare_request`，每轮工具循环均调用） | 不需要改动；现有统一入口已覆盖正常请求与 finalize 请求 |
| 工具循环达到输入预算 80% 提前压缩 | 已实现 | `mini_harness/agent/compaction.py`（`compact_soft_ratio`、`prepare_request` 中 `soft_exceeded`） | 保持不变（保留既有 80% 策略） |
| 超预算时 Tool Result 治理与确定性回退 | 已实现 | `mini_harness/agent/compaction.py`（`truncate_tool_results`、`_apply_pressure`、`_deterministic_fit`） | 保持不变 |
| 保留 Session 历史与 LLM 上下文分离 | 已实现 | `mini_harness/agent/loop.py`（append-only Session 更新）、`mini_harness/agent/runner.py`（`new_messages` 边界） | 保存 Session 时保留 summary 字段；partial failure 路径同步保留 |
| 历史摘要持久化 | 未实现 | `ContextCompactor` 摘要只存在本次 Governor 内存缓存；`Session` 无摘要字段 | 新增 Session Summary Checkpoint 与 `SessionCompactor` 持久化流程 |
| 摘要跨进程恢复 | 未实现 | `SessionManager.load()` 只恢复 `messages` | `load/save` 读写摘要与归档边界；`ContextBuilder` 重启后直接复用 |
| `/compact` 命令 | 未实现 | `mini_harness/cli/app.py` 命令表无 `/compact` | 新增命令，调用统一 `SessionCompactor` |
| Idle Compaction | 未实现 | 无后台任务/空闲检查 | 新增 `IdleCompactionScheduler`，默认 15 分钟/60 秒检查，0 可关闭 |
| Summary Checkpoint（原始历史+归档边界+摘要+未归档消息） | 未实现 | `Session` 只有 `messages` | 增加字段并保证“摘要写入+边界更新”单次原子保存 |
| Memory Archive（MEMORY.md / history.jsonl / .dream_cursor） | 未实现 | 无 memory 包 | 新增 `MemoryStore`，cursor 单调、checkpoint 幂等 |
| Dream 自动/手动整理长期记忆 | 未实现 | 无 Dream 代码 | 新增 `DreamManager` + `DreamScheduler` + `/dream`，结构化 JSON 操作、校验、去重、失败不推进 cursor |
| 摘要重复注入 / 重复压缩风险 | 当前无（因为摘要不持久化）；新增后必须防护 | `ContextBuilder` 目前每轮从完整 `history` 选择 | 请求只使用 `history[summary_boundary:]`；同一 checkpoint 不重复归档；同一 Session 压缩锁互斥 |
| 消息丢失风险 | 当前无；新增压缩不得破坏 | `Session` 保存完整原始历史 | 压缩只移动边界，不删消息；失败路径不写 Session |
| 并发更新风险 | 部分（CLI 有 `flock` 单实例锁） | `SessionManager.lock()` 仅保护 CLI 进程 | 新增 Session 压缩锁、Dream 锁与 MemoryStore 进程内写锁；Dream 与 Compaction 文件职责分离 |

## 关键结论

1. **Token Compaction 已有实现质量较高，直接复用**：`ContextGovernor.prepare_request` 已在
   `AgentRunner._request` 的统一入口被每次调用；软阈值 80%、硬阈值回退、Tool Result 截断、
   Provider 输出额度同步均已存在，本次不重写。
2. **真正缺失的是“持久化语义”**：现有摘要只影响一次请求，Session 中没有摘要与归档边界，
   因此重启后无法恢复，也没有 `/compact`、Idle Compaction、Dream。
3. 新增设计严格保持两个不变量：
   - `Session.messages` 永远是完整原始历史；
   - 模型请求只发送 `System Prompt + Long-term Memory + Session Summary + 未归档历史 + 当前消息`。
4. 现有 `_persist_partial_failure` / `_persist_result_failure` 在新增 Session 字段后必须显式保留
   summary 字段，否则失败恢复路径会丢摘要；本次一并修复。
5. CLI `_effective_context_config` 重建 `ContextConfig` 时必须传播新增配置项，否则切换模型后
   Idle/Dream 配置会回退默认值；本次一并修复。

## V2.2 实现后状态（本次交付）

| 能力 | 状态 | 对应源码 |
|---|---|---|
| 请求内 Token Compaction（80% 软阈值、硬阈值摘要/确定性回退） | 已实现（复用，不改算法） | `mini_harness/agent/compaction.py` |
| Session Summary Checkpoint（summary + boundary + checkpoint_id，原子保存/重启恢复） | 已实现 | `mini_harness/session/manager.py`、`mini_harness/agent/session_compaction.py` |
| 请求结构 System + Long-term Memory + Summary + Unarchived History + Current | 已实现 | `mini_harness/agent/context.py`、`mini_harness/agent/loop.py` |
| `/compact` 手动持久化压缩 | 已实现 | `mini_harness/cli/app.py`、`mini_harness/agent/session_compaction.py` |
| Idle Compaction（默认 15 分钟 / 60 秒检查，0 关闭） | 已实现 | `mini_harness/agent/maintenance.py` |
| Memory Archive（MEMORY.md / history.jsonl / .dream_cursor） | 已实现 | `mini_harness/memory/store.py` |
| Dream 自动/手动长期记忆整理（结构化 JSON、去重、失败不推进 cursor） | 已实现 | `mini_harness/memory/dream.py`、`mini_harness/cli/app.py` |
| 12 项验收场景自动化测试 | 已实现 | `tests/test_memory_acceptance.py` 等，全量 396 passed |
