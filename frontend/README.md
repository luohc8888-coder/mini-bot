# Mini Harness Code Explorer — Frontend

V3.0 Phase 4 的 React + React Flow 前端。正式运行时由 `mini-bot web` 的单个 FastAPI 服务提供，通过 `/api/project` 和 `/api/graph` 加载真实 CodeGraph 数据；节点可按 项目 → 模块 → 类 → 方法 下钻，并通过面包屑返回。

要求：Node.js >= 20.19（本项目在 Node 26 上验证）。

```bash
cd frontend
npm install
npm run build
```

构建流程会执行 TypeScript 类型检查、Vite 生产构建，并把产物同步到 `mini_harness/web/static`。随后从仓库根目录启动：

```bash
cd ..
mini-bot web
# 打开 http://127.0.0.1:8765
```

开发模式（可选）：

```bash
# 终端 1：启动后端 API
mini-bot web

# 终端 2：启动 Vite，开发服务器会把 /api 代理到 127.0.0.1:8765
cd frontend && npm run dev
```

后续阶段：

- Phase 5：源码面板替换为 Monaco Editor。
- Phase 6：AI 解释面板接入现有 `LLMProvider`。
