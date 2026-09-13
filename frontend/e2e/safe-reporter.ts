import {unlinkSync} from "node:fs";
import {basename, isAbsolute, relative, resolve} from "node:path";
import type {FullConfig, FullResult, Reporter, Suite, TestCase, TestResult}
  from "@playwright/test/reporter";

const testStatuses = new Set(["passed", "failed", "timedOut", "skipped", "interrupted"]);
const runStatuses = new Set(["passed", "failed", "timedout", "interrupted"]);

export default class SafeReporter implements Reporter {
  private cases = new Map<string, number>();
  private outputs: string[] = [];
  private runnerErrors = 0;

  printsToStdio() { return true; }

  onBegin(config: FullConfig, suite: Suite) {
    this.outputs = config.projects.map((project) => resolve(project.outputDir));
    suite.allTests().forEach((test, index) => this.cases.set(test.id, index + 1));
    console.log(`AEGIS_E2E begin cases=${this.cases.size}`);
  }

  onTestEnd(test: TestCase, result: TestResult) {
    const name = test.parent.project()?.name;
    const project = name === "mobile-chromium" || name === "mobile-webkit" ? name : "unknown";
    const status = testStatuses.has(result.status) ? result.status : "unknown";
    const line = Number.isInteger(test.location.line) ? test.location.line : 0;
    console.log(`AEGIS_E2E project=${project} case=${this.cases.get(test.id) ?? 0} line=${line} status=${status}`);
    // The safe fixture already removed free-text error details before capture.
    // Retain only explicit masked screenshots, never automatic error contexts.
    for (const attachment of result.attachments) {
      if (!attachment.path || basename(attachment.path) !== "error-context.md") continue;
      const path = resolve(attachment.path);
      if (!this.outputs.some((output) => {
        const child = relative(output, path);
        return child !== ".." && !child.startsWith("../") && !isAbsolute(child);
      })) continue;
      try {
        unlinkSync(path);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
          throw new Error("Automatic browser context cleanup failed.");
        }
      }
    }
  }

  onError() {
    if (++this.runnerErrors <= 5) console.log("AEGIS_E2E runner-error diagnostics=withheld");
  }

  // Worker output can include credential values just like timeout messages.
  onStdOut() { /* Free-text output deliberately withheld. */ }
  onStdErr() { /* Free-text output deliberately withheld. */ }

  onEnd(result: FullResult) {
    const status = runStatuses.has(result.status) ? result.status : "unknown";
    console.log(`AEGIS_E2E end status=${status}`);
    // No returned status override: an intentionally failed test remains failed.
  }
}
