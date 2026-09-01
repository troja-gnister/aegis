import react from "@vitejs/plugin-react";
import {defineConfig} from "vite";
import type {UserConfig} from "vite";

type TestConfig = {
  environment: "jsdom";
  globals: boolean;
  setupFiles: string[];
};

type AegisConfig = UserConfig & {test: TestConfig};

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
} as AegisConfig);
