/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // The dev server proxies /api to the local TradeGuard API (`tradeguard api`, port 8000),
  // so the browser talks to one origin and no CORS setup is needed. loadEnv (not
  // `process`) keeps Node's types out of this browser project.
  const apiTarget = loadEnv(mode, ".", "").TRADEGUARD_API || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
          timeout: 15 * 60 * 1000, // a chat turn can wait minutes for a human approval
          proxyTimeout: 15 * 60 * 1000,
        },
      },
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      css: false,
    },
  };
});
