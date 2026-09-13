import {defineConfig} from "@playwright/test";
import {fileURLToPath} from "node:url";

const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:18080";

export default defineConfig({
  testDir: "./e2e",
  outputDir: "test-results",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: [[fileURLToPath(new URL("./e2e/safe-reporter.ts", import.meta.url))]],
  use: {
    baseURL,
    browserName: "chromium",
    viewport: {width: 390, height: 844},
    deviceScaleFactor: 1,
    hasTouch: true,
    isMobile: true,
    locale: "en-US",
    // Traces include session cookies and credential POST bodies. Retain only
    // explicitly masked screenshots from synthetic fixture pages.
    screenshot: "off",
    trace: "off",
    video: "off",
  },
  projects: [
    {name: "mobile-chromium", use: {browserName: "chromium"}},
    {name: "mobile-webkit", use: {browserName: "webkit"}},
  ],
});
