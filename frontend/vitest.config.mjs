import { defineConfig } from "vitest/config";
export default defineConfig({
  test: {
    environment: "node",
    pool: "threads",
    maxWorkers: 2,
    include: ["tests/**/*.test.ts"],
  },
});
