import { resolve } from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The built app ships inside the Python package (served by `mizzle studio`).
  build: {
    outDir: resolve(import.meta.dirname, "../src/mizzle/studio/static"),
    emptyOutDir: true,
  },
  // During `pnpm dev`, forward API calls to a running `mizzle studio --port 4983`.
  server: { proxy: { "/api": "http://127.0.0.1:4983" } },
  resolve: {
    alias: {
      "@": resolve(import.meta.dirname, "./src"),
    },
  },
})
