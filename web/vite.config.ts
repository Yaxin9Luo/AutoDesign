import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // ESM-native form — works in Node 20+ without @types/node.
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // Pin to a single canonical origin. `localhost` and `127.0.0.1` are
    // distinct browser origins → distinct localStorage / IndexedDB
    // buckets, which would otherwise show two completely different
    // conversation histories depending on which URL you opened. We
    // standardize on 127.0.0.1 (matches the README, screenshots, and
    // the FastAPI proxy target below) and keep `strictPort` so a busy
    // 5173 fails loudly instead of silently drifting to 5174.
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // Proxy /api/* to the Python FastAPI server (scripts/web_server.py).
    // Same-origin from the browser's view → no CORS config needed.
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
