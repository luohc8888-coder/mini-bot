import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 正式运行时前端由 FastAPI 静态服务提供；开发模式下 Vite 通过代理访问
// 同一个 `mini-bot web` 的 /api，避免维护第二套数据源。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
  },
});
