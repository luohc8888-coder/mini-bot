# Code Explorer · AST 索引与 CodeGraph

Phase 2/3 的 Python AST 索引与符号关系图实现。它只做静态分析，不执行目标项目代码，也不调用 LLM。

## 运行

```bash
# 从项目根目录扫描当前 Workspace
python -m mini_harness.explorer --workspace . --save

# 输出完整索引 JSON 到 stdout
python -m mini_harness.explorer --workspace . --json

# 构建 CodeGraph 并输出图统计
python -m mini_harness.explorer --workspace . --graph

# 输出完整 CodeGraph JSON
python -m mini_harness.explorer --workspace . --graph-json
```

索引默认保存到：

```text
~/.mini-harness/explorer/<workspace_id>/index.json
```

## 结构

```text
mini_harness/explorer/
├── models.py       # ProjectIndex / SymbolRecord / ImportRecord / CallRecord
├── base.py         # LanguageAdapter 抽象
├── ast_parser.py   # Python ast 适配器
├── scanner.py      # 文件遍历、忽略规则、路径安全、大小限制
├── codegraph.py    # ProjectIndex -> CodeGraph 构建与查询
├── store.py        # 索引原子持久化
└── __main__.py     # 手动扫描入口
```

## 索引内容

- 文件：相对路径、语言、大小、SHA-256、行数、解析状态；
- 符号：模块 / 类 / 函数 / 方法，带 qualifiedName、起止行、签名、docstring、装饰器、父节点；
- import：模块、名称、别名、相对层级、位置；
- calls：调用者符号、原始调用表达式、位置；
- diagnostics：语法错误、超大文件、编码错误和安全的符号链接跳过记录。

## CodeGraph

`build_code_graph(index)` 将 `ProjectIndex` 转换为可查询的 `CodeGraph`：

- `contains`：模块 → 类/函数，类 → 方法/嵌套类等层级关系；
- `imports`：项目内部模块之间的 import 关系，支持相对 import 和别名；
- `calls`：能静态确定的函数/方法调用；
- `unresolved`：无法静态确定来源的调用和内部 import，不伪造动态边。

查询接口包括 `get_node()`、`get_children()`、`get_callers()`、`get_callees()`、`get_neighbors()` 和 `stats`。

## 多语言扩展

`LanguageAdapter` 是语言无关扩展点。未来新增 Java/C++/TypeScript 时：

1. 新增 `LanguageAdapter` 子类；
2. 声明 `language` 和 `extensions`；
3. 在 `parse()` 中输出统一的 `SymbolRecord` / `ImportRecord` / `CallRecord`；
4. 创建 `ProjectScanner(workspace, adapters=[...])`。

当前阶段只实现 Python 适配器，Java/C++/前端的适配器不在此阶段范围内。
