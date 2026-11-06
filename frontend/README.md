# Mini Harness Code Explorer — Frontend

Phase 1 的可交互 UI 原型。当前使用 Mock JSON，不连接 Python AST 或 FastAPI。

要求：Node.js >= 20.19（本项目在 Node 26 上验证）。

```bash
cd frontend
npm install
npm run dev
# 打开 http://127.0.0.1:5173
```

生产构建：

```bash
npm run build   # TypeScript 类型检查 + Vite 构建到 dist/
npm run preview
npm run test:mock
```

后续阶段：

- Phase 4：构建产物由 `mini-bot web` 的单进程 FastAPI 服务提供。
- Phase 5：源码面板替换为 Monaco Editor。
- Phase 6：AI 解释面板接入现有 `LLMProvider`。
