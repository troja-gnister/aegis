import react from "@vitejs/plugin-react";
import {defineConfig} from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    include: [
      "src/**/*.test.{ts,tsx}",
      "e2e/safe-reporter.unit.ts",
      "e2e/webkit-endpoint.unit.ts",
    ],
    css: true,
    environment: "jsdom",
    environmentOptions: {
      jsdom: {
        url: "http://localhost/",
      },
    },
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
