# V2.2 独立 QA 与代码审查报告

- 审查对象：`mini-harness` V2.2 Session Summary Checkpoint / Memory Archive / Dream / Idle Compaction
- 审查方式：独立 QA（只读取代码、运行测试与临时脚本；未修改任何源码或测试源码）
- 审查时间：2026-11-05
- 当前工作区说明：已复核 Lead 最近修复的 CLI `--idle-compact-after-minutes 0`、`--no-dream` 启动生效问题，以及 `wait_for_check_completion` 防覆盖逻辑，结论以当前工作区为准。

## 1. 测试结果

### 1.1 全量测试

```text
命令：cd mini-harness && .venv/bin/python -m pytest -q
结果：391 passed in 6.34s
退出码：0
```

### 1.2 V2.2 新增测试（按文件）

```text
tests/test_session_summary.py       16 passed
tests/test_memory_store.py          21 passed
tests/test_dream.py                 24 passed
tests/test_session_compaction.py    17 passed
tests/test_idle_compaction.py       12 passed
tests/test_context_summary.py        7 passed
tests/test_loop_summary.py           2 passed
tests/test_cli_memory_commands.py    6 passed
tests/test_memory_acceptance.py     12 passed
合计：117 passed in 0.60s
```

### 1.3 手工验证脚本

使用临时脚本 `/tmp/qa_manual.py`（不写入项目）验证：

- `[PASS]` 重启恢复 Summary：重启后 ContextBuilder 只注入一条 summary，已归档原始历史不重复发送。
- `[PASS]` 并发 compact + dream：cursor 单调、MEMORY.md 与 Session JSON 均合法，无文件损坏。
- `[PASS]` 超大历史多轮压缩：8 次连续 `compacted`，最终 `nothing_to_compact`，boundary 完整覆盖全部完整轮次，无丢消息标记。
- `[PASS]` CLI 维护参数：`--idle-compact-after-minutes 0` / `--no-dream` 实际不创建后台任务；`idle=5` / `dream_interval=0.5` 实际生效。
- `[PASS]` 前缀选择一致性：120 组随机配置中 summary prefix 与真实 transcript 无偏差。
- `[PASS]` Idle 压缩期间 mark_active：新回合等待进行中的压缩完成，不覆盖 Session。
- `[FAIL/EXPECTED]` stale `.dream_cursor` 探针：见问题 B-2（这是用于暴露问题的预期失败探针）。

## 2. 逐项核对表（需求 → 证据）

| 需求 | 证据 | 结论 |
|---|---|---|
| Session 新增 `summary` / `summary_boundary` / `summary_checkpoint_id`，保存完整 messages | `mini_harness/session/manager.py` 的 `Session` 定义与 `save()`：单次 JSON 原子替换；`test_session_summary.py` 16 项通过；重启手工验证通过 | PASS |
| summary 写入和 boundary 更新原子；崩溃/保存失败时旧文件不被破坏 | `SessionManager.save()` 先序列化校验，再同目录临时文件 + `fsync` + `os.replace`（`manager.py` 约 253-321 行）；`test_session.py::test_save_failure_keeps_previous_file_intact`；重启恢复脚本 | PASS |
| 旧 schema v1 文件缺省 summary 字段兼容；非法 boundary 拒绝 | `manager.py` 的 `load()` 缺省值与 `_validate_summary()`（约 470-508 行）；`test_session_summary.py` 覆盖 | PASS |
| `MemoryStore` cursor 单调、checkpoint 幂等、MEMORY.md/history 原子写、损坏行跳过、`.dream_cursor` 持久化 | `mini_harness/memory/store.py`：`append_history()` 142-210 行、`read_history()` 212-247 行、`_atomic_write_text()` 371-412 行；`test_memory_store.py` 21 项通过 | PASS |
| ContextBuilder 请求结构：System Prompt（含 MEMORY.md）+ 一条 Summary + 未归档 History + Current | `mini_harness/agent/context.py` `build_context()` 约 540-680 行；`build_session_summary_message()` 约 934-963 行；`test_context_summary.py` 7 项通过 | PASS |
| 不重复发送已归档历史、不重复注入 summary | `history[summary_boundary:]` 切片 + 单条 summary message；手工重启脚本与 `test_context_summary.py` 均通过 | PASS |
| Tool Call / Tool Result 配对不被破坏 | `ContextBuilder._select_history()` / `_split_history_into_turns()`；120 组工具轮次随机 fuzz 通过；全量旧测试通过 | PASS |
| SessionCompactor 只压缩完整轮次；无完整轮次不调 LLM；失败不改 Session | `mini_harness/agent/session_compaction.py` `_complete_turn_groups()` 101-130 行、`compact()`/`_compact_locked()` 296-499 行；`test_session_compaction.py` 17 项通过 | PASS |
| 大历史压缩不漏摘要却推进 boundary | `_select_summary_prefix()` 514-573 行按真实 transcript 预算选择前缀；`_build_summary_input()` 575-597 行；手工 8 轮超大历史通过，最终 boundary 覆盖完整历史 | PASS |
| 并发同一 Session compact 返回 busy，不重复调用 LLM | 模块级 `async with _lock_for(...)`；`test_session_compaction.py` 并发用例；全量通过 | PASS |
| Idle 15 分钟、活跃 Session 跳过、执行前复查空闲、检查期间不覆盖新回合 | `mini_harness/agent/maintenance.py` `check_once()`/`_check_once_locked()` 139-205 行；`wait_for_check_completion()` 126-137 行；`test_idle_compaction.py` 12 项通过；手工 idle race PASS | PASS |
| Dream 无新记录不调 LLM；失败/非法 JSON/写入失败不推进 cursor；MEMORY.md 内容受约束 | `mini_harness/memory/dream.py` `_run_locked()` 449-588 行；`test_dream.py` 24 项通过；并发 compact+dream 手工通过 | PASS |
| Dream 去重、update/remove、重启 cursor 恢复、busy 互斥、max_entries 限制 | `dream.py` `_apply_operations()` 及 `DreamManager`；`test_dream.py` 覆盖；全量通过 | PASS |
| CLI `/compact`、`/dream` 成功/无需/失败/忙输出；后台任务启动与取消 | `mini_harness/cli/app.py` `_handle_compact_command()` 508-542 行、`_handle_dream_command()` 544-575 行、`_start_maintenance()`/`_stop_maintenance()` 432-506 行；`test_cli_memory_commands.py` 6 项通过 | PASS |
| CLI 参数 `--idle-compact-after-minutes 0`、`--no-dream`、`--dream-interval-hours` 生效 | 当前 `_start_maintenance()` 显式使用 override 字段（432-485 行）；实际 `run()` 探针：`idle=0/no_dream` → `(None, None)`；`idle=5/interval=0.5` → `(5.0, 0.5)` | PASS（已验证 Lead 修复） |
| AgentLoop 保存 summary 字段；partial failure 路径保留；Session 模式自动创建 MemoryStore | `mini_harness/agent/loop.py` 78-118 行、404 行、443-478 行；`test_loop_summary.py` 与全量通过 | PASS |
| 全量回归、旧 V0/V1 路径兼容 | 391 passed in 6.34s；旧测试全部通过 | PASS |

## 3. 问题分级

### 阻断（Blocker）

无。

### 重要（Important）

#### B-1 Idle Compaction 失败后每隔检查周期重复调用 LLM，没有退避/失败记忆

- 文件/行号：
  - `mini_harness/agent/maintenance.py:163-205`（`_check_once_locked()`；第 203 行直接 `await self.compactor.compact(reason="idle")`，失败结果被丢弃）
  - `mini_harness/agent/compaction.py:544`（`摘要未减少上下文 Token，已放弃本次 Compact`）
- 现象：上一版 summary 很短、新完整轮次很小时，摘要输出无法减少 Token，`SessionCompactor.compact()` 每次返回 `failed`；Session mtime 不变，`IdleCompactionScheduler` 下一检查周期（默认 60 秒）会再次调用 Provider。Provider 临时故障时同样会每 60 秒重试，形成后台 LLM 重试风暴；无 failure memo、无 backoff，且不会推进 cursor。
- 复现（临时脚本，已在当前工作区跑通）：
  ```python
  # 关键步骤：构造 summary="旧摘要：已确认 P0-1 修复并保留路径。"，boundary=2，
  # 后面追加 [{"role":"user","content":"谢谢"},{"role":"assistant","content":"不客气"}]
  # 将 Session mtime 设为 20 分钟前，再连续调用 3 次：
  for _ in range(3):
      result = await scheduler.check_once()
  # 实际输出：provider.calls == 3
  # statuses == [('failed','摘要未减少上下文 Token，已放弃本次 Compact'), ... x3]
  ```
- 建议：为每个 Session 记录最近一次失败的 `mtime + summary_boundary + summary hash`，失败后在同一 Session 未变化前不再重试；或给 Idle 失败增加指数退避；同时把“非减少摘要”视为可跳过的稳定状态而不是可重试错误。

#### B-2 `.dream_cursor` 大于当前 history 最大 cursor 时，Dream 永久静默跳过所有新记录

- 文件/行号：
  - `mini_harness/memory/store.py:212-247`（`read_history(since_cursor=...)` 只返回 `cursor > since_cursor`）
  - `mini_harness/memory/dream.py:449-478`（`_run_locked()` 使用 `get_dream_cursor()` 后直接 `read_history`，空 batch 直接返回 `processed_entries=0`）
- 现象：如果用户清理/重置了 `history.jsonl`（或从旧 workspace 复制），`.dream_cursor` 仍为 99，而新记录 cursor 从 1 开始；Dream 永远读不到新记录，`llm_called=False`，无错误提示，长期记忆停止更新。
- 复现：`set_dream_cursor(99)` → `append_history(...)`（新记录 cursor=1）→ `DreamManager.run()`；实际 `processed_entries=0, cursor=99, llm_called=False, error=None`。
- 建议：`DreamManager` 启动时若 `current_cursor > store.latest_cursor()`，将 cursor 回退到 `latest_cursor()`（或至少记录 warning 并报警）；`set_dream_cursor` 也可在设置时校验上界。

### 建议（Suggestion）

1. **归档失败后无重试入口**：`SessionCompactor._compact_locked()` 已先保存 Session（boundary 已推进），`MemoryStore.append_history()` 失败时后续 `compact()` 会因 `nothing_to_compact` 永久不再归档该摘要（`session_compaction.py:466-484`）。设计 §4 明确允许归档失败不影响 Session 成功，但建议在 Session 中记录 `archive_pending` 或启动时重试，避免长期记忆永久缺一段。复现：模拟 `append_history` 抛 `MemoryStoreError("disk full")`，首次返回 `status=compacted, archive_cursor=None`，再次 compact 返回 `nothing_to_compact`，history 为空。
2. **失败轮次位于中间时，后续完整轮次无法再被持久化压缩**：`_complete_turn_groups()` 遇第一个不完整轮次即停止（`session_compaction.py:101-130`）。例如 turn2 因预算失败被 partial-failure 保存为以 tool 结尾的未完成轮次，turn1 可先压缩，但 boundary 停在 turn2 前，turn3 及以后永远无法进入 summary。设计上“保留当前未完成请求”合理，但建议明确该长期退化行为，或为已放弃的失败轮次提供标记/跳过策略。
3. **Dream 输入没有 Token 预算**：`DreamManager._build_user_message()`（`dream.py:597-617`）把完整 MEMORY.md（最大 64KB）+ 最多 20 条归档摘要一次性发送；小上下文模型可能反复因请求过大失败且 cursor 永不推进。建议增加输入截断/分批。
4. **Dream 的 `(nothing)` 识别过窄**：`dream.py:379-381` 只识别精确的 `(nothing)`（大小写不敏感）；模型输出 `(nothing)。`、`（nothing）` 或其他“无内容”表达会被当非法 JSON，每次 Dream 失败、cursor 不推进。建议先 strip 常见标点和全角括号。
5. **`summary_checkpoint_id` 拼接存在理论歧义**：`session_compaction.py:211-218` 使用 `f"{session_key}{boundary}{summary}"` 直接拼接后哈希；不同 tuple 理论上可拼出同一字符串（例如 boundary/summary 边界组合）。建议用带分隔符的 JSON/元组序列化。
6. **ContextBudgetExceeded 提示重复统计长期记忆**：`context.py:70-85` 中 `system_tokens` 已包含 MEMORY.md，但错误文本又单独列出 `long_term_memory`，用户会看到 System Prompt 与 Long-term Memory 被重复相加；实际预算计算没有重复，只是提示口径问题。
7. **AgentLoop 的自动 MemoryStore 已修复并确认可用**：当前 `loop.py:78-118` 不再要求 `context_builder is None`，自定义 Builder 也会被挂载默认 MemoryStore；此前版本的问题已消失，记录为回归确认项。

## 4. 最终结论

- **无阻断问题**。
- **存在 2 个重要问题**：Idle 失败重试风暴（B-1）、stale `.dream_cursor` 导致 Dream 永久跳过新记录（B-2）。
- 核心需求已基本满足：Session summary 原子保存/恢复、ContextBuilder 单条 summary + 未归档历史、Tool 配对、SessionCompactor 完整轮次与 busy、Idle active/recheck、Dream 无新不调 LLM/失败不推进 cursor/去重/重启恢复、CLI 命令输出与任务生命周期均通过测试与手工验证。
- 全量测试：**391 passed，0 failed**。
- 建议优先修复 B-1、B-2，其余建议可按迭代处理；本次审查未修改任何源码或测试源码。

## 5. Lead 修复记录（QA 报告之后）

- B-1（Idle 失败重试风暴）：`mini_harness/agent/maintenance.py` 增加失败指纹（mtime + summary_boundary + summary hash）与指数退避（60s 起，最大 1h）；同一 Session 内容未变化时不再每周期调用 LLM。新增 `test_idle_failure_backoff_avoids_llm_retry_storm`。
- B-2（stale `.dream_cursor`）：`mini_harness/memory/dream.py` 检测 `cursor > latest_cursor` 后回退到 0 重放历史，由 MEMORY.md 去重保证幂等。新增 `test_stale_dream_cursor_is_clamped_to_latest_history`。
- 建议 1（归档失败无重试）：`SessionCompactor.has_unarchived_history()` 识别“Summary 已保存但 checkpoint 未归档”的待补偿状态；无完整轮次时只补归档、不调用 LLM。新增 `test_pending_archive_is_retried_without_llm`。
- 建议 4（`(nothing)` 变体）：支持全角括号、结尾标点及常见空操作表达。新增 `test_nothing_variants_are_accepted`。
- 建议 5（checkpoint ID 歧义）：改为 JSON 数组序列化后哈希；同步更新既有测试。
- 建议 6（预算提示重复统计）：改为 `System Prompt X（其中 Long-term Memory Y）`。
- 修复后全量测试：**396 passed, 0 failed**。

## 6. 本轮复核与补充修复（2026-11-05）

在工作区现有修复基础上补齐以下行为，保留前述 QA 的历史结论：

- **B-1**：区分临时失败与稳定失败。Provider 临时失败继续指数退避，计时从失败完成时开始；摘要未减少 Token、完整轮次放不进摘要预算时，Session 指纹不变就不再自动重试。新回合保存后恢复尝试，手动 `/compact` 不受后台退避限制。
- **B-2**：陈旧 cursor 只改变本次历史读取起点，不在调用 LLM 前提前写成 0。Provider 失败、非法响应或禁用本次处理时，旧 cursor 保持不变；成功后才持久化处理到的新 cursor。
- **建议 1**：补归档移到新版摘要生成之前。即使归档失败后有新回合到来，也先幂等归档旧 checkpoint；补偿失败则保留旧检查点，不调用摘要模型，不覆盖恢复入口。
- **建议 2**：已有后续 user 的历史失败轮次允许进入摘要，明确标注“未产生最终回答”。最新未完成轮次仍保留，非法工具协议仍不放行；原始消息与工具配对均不改写。
- **建议 3**：Dream 按模型窗口及 CLI 上下文配置计算输入预算，按实际剩余窗口限制输出。完整归档按连续前缀分批；超大单条归档分片覆盖全部正文，所有片段成功后统一提交记忆及 cursor。长期记忆仅裁剪发送副本，完整文档用于操作校验和去重；提示本身放不下时不请求模型。有效 JSON 若带输出截断结束原因，也按失败处理。
- **建议 4**：补充 `( nothing )；` 等空白和标点变体；建议 5、6、7 的既有修复继续通过回归。
- **并发请求预算**：新增 Provider 协程作用域输出额度，Dream 与主对话共用连接时互不覆盖，异常和取消后恢复；进行中的 Dream 固定使用启动时的 Provider。

验证：新增 **20 项回归用例**，覆盖失败轮次、跨新回合补归档、真实时间推进的退避、陈旧 cursor 失败恢复、分片完整性与统一提交、预算不足、输出截断，以及 OpenAI-compatible / Anthropic 模拟 HTTP 请求预算与协程隔离。

```text
.venv/bin/python -m pytest -q
418 passed in 6.54s

.venv/bin/python -m mini_harness --demo
结果是 5

git diff --check
通过
```

本轮验证使用本地测试与模拟 HTTP，不包含真实模型调用。

## 7. 结构与旧兼容清理后的回归（2026-11-05）

前述 QA 核对表记录当时的实现快照；本节为后续结构清理后的状态。

- 删除根目录 `main.py`、`agent/prompt.py`、`session/workspace.py` 及相关旧导出；统一模板、Workspace 和 CLI 入口。
- 删除重复的请求快照字段和 CLI 无状态交互循环。正式 CLI 始终绑定 Session；离线 demo 显式使用 `stateless=True`。
- `ContextCompactor.compact()` 统一返回 `CompactionOutcome`；去掉 `last_outcome` 和两类返回值适配。预算 Governor、Token/消息处理、共享 transcript 独立成模块。
- CLI 渲染/设置/维护生命周期、Memory 文档操作/调度、AddTool 示例按职责分开；不保留旧模块转导。
- Session 正常加载严格使用 schema v2。旧 v1 数据通过 `--migrate-session` 单独校验、备份和原子迁移；当前工作区的 102 条消息和已有摘要已核对保留。
- 原有功能测试覆盖保留；新增 18 项迁移测试及 2 项显式无状态模式约束测试。
- 全量 **438 passed**；模块入口与已安装的 `mini-bot --demo` 均返回 `结果是 5`；差异格式检查通过。

完整目录、接口与迁移说明见 [代码结构与兼容清理结果](code_structure_and_compatibility.md)。
