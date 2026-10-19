import { defineConfig } from "vite-plus";

export default defineConfig({
  server: {
    port: 5173,
    // В разработке API отдаёт FastAPI (python -m server).
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  build: {
    target: "es2022",
    // Ассеты получают хэш в имени — сервер отдаёт их с immutable-кэшем на год.
    assetsInlineLimit: 0,
  },
  lint: {
    ignorePatterns: ["dist/**", "node_modules/**"],
    options: { typeAware: true, typeCheck: true },
  },
  fmt: {
    printWidth: 110,
    ignorePatterns: ["dist/**", "node_modules/**"],
  },
});
