# 长期记忆

## 项目决策
- `mini_harness/prompts/defaults.py` 明确引导：按文件名或路径模式查找使用 `find_files`；同一批多处修改优先使用 `apply_patch`，确保全部编辑先通过校验再写入。

## 架构约定
- `mini_harness/cli/app.py` 的生产工具注册表现有 10 个工具：`add`、`list_dir`、`grep`、`read_file`、`write_file`、`edit_file`、`file_info`、`run_command`、`find_files`、`apply_patch`。
- 工具 Registry 统一执行超时 130 秒，输出超过 32k 截断，并将异常转换为 `ToolResult.error`。
- `apply_patch` 使用 `edits` 数组，每项为 `{path, old_text, new_text, replace_all?}`；它先在内存中按顺序校验并应用全部编辑，全部通过后才写入。任一项失败都不会修改文件。
- `apply_patch` 会保留文件原有 CRLF/LF 风格和权限位，单文件上限 2 MiB，单次最多 100 条编辑。
- `find_files` 支持 `path`、`pattern`（glob）、`kind=file|dir|any`、`max_results` 参数，并复用 `filesystem.py` 的 `_IGNORE_DIRS` 与 `Workspace.resolve_path()`。
- `mini_harness/tools/base.py` 的 `validate_arguments` 已支持递归校验嵌套 object/array 的 `required`、类型、`enum`、数值上下界、`additionalProperties`、`minItems`/`maxItems`，嵌套错误以 `edits[0].path` 形式定位。

## 长期目标
- 完成工具扩展收尾：复跑 `uv run pytest -q` 全量回归，并同步更新 `README.md` 与 `docs/tools_gap_analysis.md`；后者仍写 8 个工具，且仍将 `apply_patch`、`find_files` 列为待做。
- P2 可选任务：将 `add` 工具从 `mini_harness/cli/app.py` 的生产注册表移到 `--demo` 路径。
- 工具缺口后续优先项为长进程会话（`start_process`/`read_process`/`stop_process`）以及文件移动、复制、删除；暂不做专用 git 工具、`web_fetch`/`web_search`、PDF/图片读取和 `todo`。
