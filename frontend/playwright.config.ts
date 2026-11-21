import { defineConfig } from "@playwright/test";

const nginx = process.env.TICKETING_SMOKE_ORIGIN;
const local = "http://127.0.0.1:4173";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: nginx ? "nginx.spec.ts" : ["smoke.spec.ts", "seat-recovery.spec.ts", "registration.spec.ts"],
  use: { baseURL: nginx ?? local, browserName: "chromium" },
  webServer: nginx ? undefined : {
    command: "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
    url: local,
  },
});
