import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {randomUUID} from "node:crypto";
import {mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {dirname, join, resolve} from "node:path";
import {fileURLToPath} from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temporary = mkdtempSync(join(tmpdir(), "aegis-browser-diagnostics-"));
const sentinel = `synthetic-diagnostic-${randomUUID()}`;
const projects = ["mobile-chromium", "mobile-webkit"];
let checkpoint = "browser process";
let diagnosticCategories = [];

function files(directory) {
  return readdirSync(directory, {withFileTypes: true}).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  });
}

try {
  // Match the real frontend ESM package when importing its configuration.
  writeFileSync(join(temporary, "package.json"), JSON.stringify({type: "module"}));
  writeFileSync(join(temporary, "probe.spec.ts"), `
import {test} from ${JSON.stringify(join(frontend, "e2e/safe-test.ts"))};
import {writeFile} from "node:fs/promises";
test("credential diagnostic probe", async ({page}, testInfo) => {
  await page.setContent('<label>Password<input type="password" disabled></label>');
  try {
    await page.getByLabel("Password").fill(process.env.E2E_ALICE_PASSWORD!, {timeout: 150});
  } catch (error) {
    await writeFile(process.env.E2E_DIAGNOSTIC_MARKER! + testInfo.project.name, "fill rejected");
    console.log(process.env.E2E_ALICE_PASSWORD);
    console.error(process.env.E2E_ALICE_PASSWORD);
    throw error;
  }
});
`);
  writeFileSync(join(temporary, "playwright.config.ts"), `
import config from ${JSON.stringify(join(frontend, "playwright.config.ts"))};
export default {...config, testDir: ${JSON.stringify(temporary)},
  outputDir: ${JSON.stringify(join(temporary, "artifacts"))}, retries: 0};
`);
  const environment = Object.fromEntries(Object.entries(process.env)
    .filter(([name]) => !name.startsWith("E2E_")));
  const result = spawnSync(process.execPath, [
    join(frontend, "node_modules/@playwright/test/cli.js"), "test", "--config",
    join(temporary, "playwright.config.ts"),
  ], {
    cwd: frontend,
    env: {...environment, E2E_ALICE_PASSWORD: sentinel,
      E2E_DIAGNOSTIC_MARKER: join(temporary, "marker-")},
    encoding: "utf8", timeout: 60_000, maxBuffer: 2 * 1024 * 1024,
  });
  assert.equal(result.status, 1, "Intentional credential-fill failures must exit nonzero");
  const output = result.stdout + result.stderr;
  diagnosticCategories = [
    ["invalid-url", "Invalid URL"], ["invalid-url-code", "ERR_INVALID_URL"],
    ["module-not-found", "Cannot find module"], ["esm-module", "ERR_REQUIRE_ESM"],
    ["fixture-definition", "Fixture"], ["syntax-error", "SyntaxError"],
    ["type-error", "TypeError"], ["config-loaded", "AEGIS_E2E begin"],
    ["test-loader-error", "AEGIS_E2E runner-error"],
    ["import-meta-commonjs", "Cannot use 'import.meta' outside a module"],
    ["import-commonjs", "Cannot use import statement outside a module"],
    ["unexpected-token", "Unexpected token"],
  ].filter(([, marker]) => output.includes(marker)).map(([name]) => name);
  checkpoint = "real browser execution";
  for (const project of projects) {
    assert.equal(readFileSync(join(temporary, "marker-" + project), "utf8"), "fill rejected",
      "The real disabled-password-fill probe must execute in each mobile browser");
  }
  checkpoint = "output redaction";
  assert.ok(!(result.stdout + result.stderr).includes(sentinel),
    "Credential value leaked into browser diagnostics");
  checkpoint = "artifact redaction";
  for (const path of files(temporary)) {
    assert.ok(!readFileSync(path).includes(Buffer.from(sentinel)),
      "Credential value leaked into retained browser artifacts");
  }
  console.log("Credential diagnostic security: Chromium and WebKit passed");
} catch {
  // Captured Playwright output may contain credentials in the RED case. Never
  // echo it, dynamic exceptions, or fixture contents into the parent runner log.
  console.error(`Credential diagnostic security regression failed: ${checkpoint}`);
  console.error(`Safe runner categories: ${diagnosticCategories.join(",") || "unclassified"}`);
  process.exitCode = 1;
} finally {
  rmSync(temporary, {recursive: true, force: true});
}
