import { defineConfig } from "vite";

export default defineConfig({
  server: {
    proxy: {
      "/api": process.env.TICKETING_ORIGIN ?? "http://localhost:8080",
      "/pay/": process.env.TICKETING_ORIGIN ?? "http://localhost:8080",
    },
  },
});
