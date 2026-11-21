import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/*.test.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**"],
      reporter: ["text-summary", "json-summary"],
      reportsDirectory: "coverage",
      // Baseline (DOM/page modules are covered by Playwright); fail if coverage drops.
      thresholds: { lines: 19, statements: 19, branches: 19, functions: 27 },
    },
  },
});
