import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 前端开发服务：/api、/ws 反代到 core（127.0.0.1:8001 环回）。
// 生产：Nginx 静态托管 + 同源反代（deploy/nginx.conf），本配置仅服务开发预览。
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    allowedHosts: [".monkeycode-ai.online"],
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8001",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
