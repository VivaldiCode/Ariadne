/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Em desenvolvimento a app corre em :5173 e fala com o server do compose (ou um local) por proxy:
// ARIADNE_URL=http://localhost:8800 npm run dev
const target = process.env.ARIADNE_URL ?? "http://localhost:8800";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": target,
      "/health": target,
    },
  },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
