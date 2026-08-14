import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API and the UI run as separate processes in development. Proxying /api
// keeps the frontend origin-agnostic, so the same build works when FastAPI
// serves it directly in production.
export default defineConfig({
  plugins: [react()],
  // Relative asset paths, because the built app is served from /app/ by
  // FastAPI but from / by the dev server. An absolute base would 404 in one of
  // the two.
  base: "./",
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
  build: { outDir: "dist" },
});
