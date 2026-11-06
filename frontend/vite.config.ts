import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Phase 1 只运行独立 Vite 前端；Phase 4 会在这里加入 /api -> FastAPI 的代理。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
  },
});
