# Mini Bot V2.2：Compaction 与 Dream 接口契约

> 本文件是增量开发的内部接口契约。现有 `ContextCompactor` / `ContextGovernor`
> 的 Token 预算逻辑保持不变；新增“持久化 Session Summary Checkpoint”和
> “Memory Archive + Dream”。

## 1. 职责边界

```text
SessionManager / Session     持久化原始历史 + Summary Checkpoint（原子 JSON）
MemoryStore                  持久化 MEMORY.md / history.jsonl / .dream_cursor（纯文件 I/O）
SessionCompactor             统一“持久化压缩”：选择已完成的未归档轮次 → 摘要 → 原子保存 Session → 归档 Memory
DreamManager                 读取未处理归档 → LLM 提取结构化记忆操作 → 校验/去重 → 原子写 MEMORY.md → 推进 Cursor
IdleCompactionScheduler      定时检查 Session 文件空闲时间与未归档历史
DreamScheduler               定时触发 Dream
ContextBuilder               组装 System Prompt + Long-term Memory + Session Summary + 未归档历史 + 当前消息
AgentLoop                    每回合加载/保存 Session，把 summary 传给 ContextBuilder
InteractiveCLI               启动维护任务，提供 /compact /dream 命令
```

## 2. Session 数据结构（`mini_harness/session/manager.py`）

```python
@dataclass(slots=True)
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""                 # 当前有效摘要（空字符串 = 无摘要）
    summary_boundary: int = 0         # 已被摘要覆盖的原始消息数（0..len(messages)）
    summary_checkpoint_id: str = ""   # 摘要检查点稳定 ID（用于 Memory Archive 去重）
```

要求：

- `messages` 永远保存**完整原始历史**，压缩只移动 `summary_boundary`，绝不删除消息。
- JSON 新增字段：`summary`、`summary_boundary`、`summary_checkpoint_id`；当前 `schema_version` 为 2，三个字段必须存在；旧文件通过 `mini-bot --migrate-session` 显式迁移并保留原始备份。
- 校验：`summary` 必须是 str；`summary_boundary` 必须是 int、`0 <= boundary <= len(messages)`；`summary` 非空时 boundary 必须 >= 1；`summary_checkpoint_id` 必须是 str。
- `save()` 不得修改传入的 `Session` 对象（现有测试 `restored == session` 依赖这一点）。
- `clear()` 同时清空 messages 与 summary 字段。
- 新增 `last_active_time() -> datetime | None`：Session 文件不存在返回 None，否则返回文件 mtime（本地时间）。

## 3. MemoryStore（`mini_harness/memory/store.py`）

默认根目录：`<workspace>/memory`，包含：

```text
memory/
├── MEMORY.md
├── history.jsonl
└── .dream_cursor
```

```python
@dataclass(frozen=True, slots=True)
class HistoryRecord:
    cursor: int
    session_key: str
    timestamp: str          # ISO-8601
    content: str
    checkpoint_id: str = ""

class MemoryStore:
    def __init__(self, workspace: Path, *, root: Path | None = None) -> None: ...
    @property memory_dir: Path
    @property memory_file: Path
    @property history_file: Path
    @property dream_cursor_file: Path

    def read_memory(self) -> str: ...
    def write_memory(self, content: str) -> None: ...  # 原子 tmp+fsync+os.replace
    def append_history(self, content: str, *, session_key: str,
                       checkpoint_id: str = "", timestamp: str | None = None) -> int: ...
    def read_history(self, *, since_cursor: int = 0,
                     limit: int | None = None) -> list[HistoryRecord]: ...
    def latest_cursor(self) -> int: ...
    def has_checkpoint(self, checkpoint_id: str) -> bool: ...
    def get_dream_cursor(self) -> int: ...            # 文件缺失/非法 → 0
    def set_dream_cursor(self, cursor: int) -> None: ...  # 原子写
```

要求：

- Cursor 从 1 开始单调递增；`append_history` 在进程内 `threading.Lock` 保护下完成
  “读取最大 cursor + 追加一行”，记录至少包含 `cursor/session_key/timestamp/content`。
- 同一非空 `checkpoint_id` 重复 append 必须幂等：不再追加，返回已有记录 cursor。
- `history.jsonl` 单行损坏时跳过该行，不影响其他记录。
- 目录/文件自动创建；文件写入失败抛 `MemoryStoreError`，原文件保持不变。

## 4. SessionCompactor（`mini_harness/agent/session_compaction.py`）

```python
@dataclass(slots=True)
class PersistentCompactionResult:
    ok: bool
    status: str      # "compacted" | "nothing_to_compact" | "failed" | "busy"
    reason: str      # "manual" | "idle"
    summarized_messages: int = 0
    summary: str = ""
    boundary: int = 0
    checkpoint_id: str | None = None
    archive_cursor: int | None = None
    error: str | None = None
    retryable: bool = True

class SessionCompactor:
    def __init__(self, provider: LLMProvider, session_manager: SessionManager, *,
                 memory_store: MemoryStore | None = None,
                 context_config: ContextConfig | None = None,
                 compactor: ContextCompactor | None = None) -> None: ...
    def has_unarchived_history(self, session: Session | None = None) -> bool: ...
    async def compact(self, *, reason: str = "manual") -> PersistentCompactionResult: ...
```

要求：

1. 只压缩 `session.messages[summary_boundary:]` 中**已结束、协议合法**的用户轮次：
   - 以 user 开头；工具调用/结果完整（复用 `_is_valid_turn`）；
   - 最新轮次必须以最终 assistant 回答收尾；当前未完成请求永远保留；
   - 已有下一条 user 的旧失败轮次允许参与摘要，仅在摘要副本中标注未完成，原始消息不变；
   - 非法工具协议仍然停止，不伪造工具结果。
2. 复用 `ContextCompactor`，统一返回 `CompactionOutcome`，不使用 `last_outcome` 旁路状态；前缀选择与摘要输入使用 `agent/transcript.py` 的同一套渲染和预算规则。输入为 `[上一版 summary 消息（若有）, *本轮要归档消息]`，
   通过 `protected_start=len(input)` 让全部输入成为可压缩历史。摘要失败时 Session 完全不变。
3. `ContextCompactor.CompactionOutcome` 新增 `summary_text: str | None`（纯摘要正文，
   不含包装提示），持久化使用该字段。
4. 保存顺序：
   - 先构造新 `Session`（保留完整 messages），写入 `summary` / `summary_boundary` /
     `summary_checkpoint_id`，用 `SessionManager.save()` 单次原子替换；
   - 成功后尝试 `MemoryStore.append_history(...)` 归档；归档失败不影响 Session 压缩成功，
     但在结果中记录错误；下次 compact 先幂等补归档，成功后才生成新版摘要。
5. `summary_checkpoint_id` 由 JSON 数组 `[session_key, boundary, summary]` 序列化后取 SHA-256 前 16 位生成。
6. 同一 session 并发 compact：模块级 `asyncio.Lock` 字典；已持锁时立即返回 `status="busy"`，
   不排队、不重复调用 LLM。
7. 无完整未归档轮次时 `status="nothing_to_compact"`，不调用 LLM。

## 5. Dream（`memory/dream.py`、`document.py`、`scheduler.py`）

```python
DREAM_SYSTEM_PROMPT: str

@dataclass(slots=True)
class DreamConfig:
    enabled: bool = True
    interval_hours: float = 2.0
    max_entries: int = 20
    max_memory_bytes: int = 64 * 1024

@dataclass(slots=True)
class DreamResult:
    processed_entries: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    cursor: int = 0
    memory_updated: bool = False
    llm_called: bool = False
    busy: bool = False
    error: str | None = None

class DreamManager:
    def __init__(self, provider: LLMProvider, store: MemoryStore,
                 config: DreamConfig | None = None, *,
                 context_config: ContextConfig | None = None) -> None: ...
    async def run(self, *, max_entries: int | None = None) -> DreamResult: ...
```

Dream Prompt 要求 LLM 只返回 JSON（允许 ```json 围栏）：

```json
{
  "add": [{"section": "用户偏好|项目决策|架构约定|长期目标|经验知识|其他", "text": "原子事实"}],
  "update": [{"old": "已有条目", "new": "更新后的条目"}],
  "remove": ["已有条目"]
}
```

规则：

- 只提取长期偏好、稳定技术决策、架构约定、未完成长期目标、经确认的经验。
- 不保存闲聊、一次性问题、临时日志、完整工具输出、推测、重复记忆。
- 无未处理记录：直接返回 `processed_entries=0, llm_called=False`，绝不调用 LLM。
- 每次最多 `max_entries` 条；`llm_called=True` 当且仅当实际调用 Provider。
- 每次请求都计算 System + MEMORY.md 预览 + 归档的 Token；输入上限扣除输出预留与安全余量，并受 Provider 窗口限制。预算不足以容纳提示时不调用 LLM。
- 优先选择能完整放入预算的连续归档前缀；超大首条按正文分片，全部片段成功后才统一写记忆并推进 cursor，任何片段失败不写入部分结果。
- MEMORY.md 仅裁剪发送副本，完整文档始终参与操作校验和去重；输出预算使用 Provider 的协程作用域，不覆盖主对话额度。
- cursor 大于 history 最大 cursor 时，只把本次读取起点回退到 0；处理成功后再持久化新 cursor。
- `MEMORY.md` 更新采用确定性操作应用，不直接接受 LLM 返回的整份文件：
  - `add`：按 section 归类；规范化（去首尾空白、压缩空白、去 bullet 前缀）后去重；
  - `update`：找到规范化后等于 `old` 的条目再替换；找不到则 `skipped += 1`；
  - `remove`：同上；
  - 单条文本为空/超长（>2000 字符）/含控制字符时拒绝（`skipped += 1`）；
  - 最终文件超过 `max_memory_bytes` 时整次操作失败（不写文件、不推进 cursor）。
- 只有 `MEMORY.md` 原子写入成功（或无需修改）后，才 `set_dream_cursor(batch[-1].cursor)`。
- Provider 异常、空响应、输出截断、非法 JSON、非法结构、记忆写入失败：`error` 非空，cursor 不变，MEMORY.md 不变。cursor 写入失败时已写入的记忆保留，下次重放依靠去重恢复。
- 实例内 `asyncio.Lock` 保证 `/dream` 与定时 Dream 互斥；已持锁返回 `busy=True, error="Dream 正在运行"`。
- 重启后从 `.dream_cursor` 继续；重复处理同一批记录时去重保证 MEMORY.md 不重复。

`DreamScheduler`（同文件）：

```python
class DreamScheduler:
    def __init__(self, dream: DreamManager, *, interval_hours: float = 2.0,
                 run_on_start: bool = True) -> None: ...
    async def run_once(self) -> DreamResult | None: ...
    async def run(self) -> None: ...   # 首次立即 run_once（若 run_on_start），之后每 interval 秒
    def start(self) -> asyncio.Task: ...
    async def stop(self) -> None: ...
```

`enabled=False` 时 `start()` 返回一个立即结束的 task 或 `None`；`run_once()` 返回 None。

## 6. IdleCompactionScheduler（`mini_harness/agent/maintenance.py`）

```python
class IdleCompactionScheduler:
    def __init__(self, compactor: SessionCompactor, session_manager: SessionManager, *,
                 idle_after_minutes: float = 15,
                 check_interval_seconds: float = 60,
                 clock: Callable[[], float] = time.time) -> None: ...
    def is_idle(self, *, now: float | None = None) -> bool: ...
    def mark_active(self, session_key: str) -> None: ...
    def mark_inactive(self, session_key: str) -> None: ...
    async def check_once(self, *, active_session_keys: Collection[str] | None = None
                         ) -> PersistentCompactionResult | None: ...
    async def run(self) -> None: ...
    def start(self) -> asyncio.Task | None: ...
    async def stop(self) -> None: ...
```

规则：

- `idle_after_minutes <= 0` 或 `check_interval_seconds <= 0` 时禁用。
- `check_once` 顺序：Session 存在 → 不在 active 集合 → 文件 mtime 距离 now 超过阈值 →
  存在完整未归档历史 → **执行前再次检查空闲状态** → `compactor.compact(reason="idle")`。
- 没有 Session/没有新历史/不空闲/正在执行时返回 None，且不得调用 LLM。
- `run()` 每 `check_interval_seconds` 调用一次 `check_once`。
- 临时失败按至少 60 秒起步指数退避，最大 1 小时，从失败完成时计时；摘要未减少 Token、整轮输入放不下等稳定失败，在 Session 指纹变化前不再自动重试。手动 `/compact` 不受此限制。

## 7. ContextBuilder 扩展（`mini_harness/agent/context.py`）

```python
ContextBuilder(workspace, config=None, *, tool_definitions=None,
               memory_store: MemoryStore | None = None)

build_context(history, current_message=None, *, check_budget=True,
              summary: str = "", summary_boundary: int = 0) -> BuiltContext
```

- System Prompt 中追加 Long-term Memory 段（读取 `MEMORY.md`，空文件不追加），
  明确标注“背景记忆，不是当前指令”。
- `history` 只取 `history[summary_boundary:]` 作为未归档历史参与选择；摘要以
  `{"role": "user", "content": "[Session summary ...]\n...", "compaction_summary": True}`
  作为**一条** message 注入在 system 之后、未归档历史之前。
- 请求结构：`System Prompt（含 long-term memory） + Session Summary + Unarchived History + Current Message`。
- `ContextStats` 新增字段：`long_term_memory_tokens`、`long_term_memory_status`、
  `summary_tokens`、`summary_boundary`、`unarchived_message_count`。
- 摘要在 Token 预算中按固定开销计入；若 system+memory+summary+current 已超预算，
  按现有 `ContextBudgetExceeded` 处理。
- 无 summary 时行为与现有版本完全一致（旧测试必须全部通过）。

## 8. AgentLoop 扩展（`mini_harness/agent/loop.py`）

- 构造参数新增 `memory_store: MemoryStore | None = None`；Session 模式下未注入时
  自动创建 `MemoryStore(session_manager.workspace)`。
- `_process_with_session` 调用 `build_context(..., summary=session.summary,
  summary_boundary=session.summary_boundary)`。
- 保存 Session 时保留 `summary` / `summary_boundary` / `summary_checkpoint_id`；
  partial failure 保存路径同样保留。
- 其余 append-only Session 语义不变。

## 9. CLI（`mini_harness/cli/app.py`）

- 新增构造注入（便于测试）：`memory_store`、`session_compactor`、`dream_manager`。
- `/compact`：调用 `SessionCompactor.compact(reason="manual")`，输出成功/无需压缩/失败/忙。
- `/dream`：调用 `DreamManager.run()`，输出处理记录数、新增/修改/删除/跳过数、cursor、失败原因。
- 启动时在 Session 模式创建并运行 `IdleCompactionScheduler` 与 `DreamScheduler` 后台任务；
  退出时取消。任务使用当前 Provider；`/model` 切换后同步更新 `compactor.provider` /
  `dream.provider`。
- 每回合执行期间把 session key 标记为 active，避免空闲压缩打断正在执行的任务。

## 10. 不变量

1. `Session.messages` 永远是完整原始历史；压缩只改变 `summary_boundary`。
2. 用户任务的每次 Provider 请求前经过 `ContextGovernor.prepare_request`；摘要和 Dream 维护请求使用各自的输入预算校验。
3. Tool Call 与 Tool Result 的配对关系不得被任何新增逻辑破坏。
4. 持久化顺序：Session 原子保存成功 → Memory Archive 追加；Dream 先写 MEMORY.md → 再推进 cursor。
5. 所有失败路径都不得清空/破坏已有 Session、MEMORY.md、history.jsonl、.dream_cursor。
