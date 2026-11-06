# Code Explorer · AST 索引层

Phase 2 的 Python AST 索引实现。它只做静态分析，不执行目标项目代码，也不调用 LLM。

## 运行

```bash
# 从项目根目录扫描当前 Workspace
python -m mini_harness.explorer --workspace . --save

# 输出完整 JSON 到 stdout
python -m mini_harness.explorer --workspace . --json
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
├── store.py        # 索引原子持久化
└── __main__.py     # 手动扫描入口
```

## 索引内容

- 文件：相对路径、语言、大小、SHA-256、行数、解析状态；
- 符号：模块 / 类 / 函数 / 方法，带 qualifiedName、起止行、签名、docstring、装饰器、父节点；
- import：模块、名称、别名、相对层级、位置；
- calls：调用者符号、原始调用表达式、位置；
- diagnostics：语法错误、超大文件、编码错误和安全的符号链接跳过记录。

## 多语言扩展

`LanguageAdapter` 是语言无关扩展点。未来新增 Java/C++/TypeScript 时：

1. 新增 `LanguageAdapter` 子类；
2. 声明 `language` 和 `extensions`；
3. 在 `parse()` 中输出统一的 `SymbolRecord` / `ImportRecord` / `CallRecord`；
4. 创建 `ProjectScanner(workspace, adapters=[...])`。

当前阶段只实现 Python 适配器，Java/C++/前端的适配器不在此阶段范围内。
