import type {FullResult} from "@playwright/test/reporter";
import {afterEach, expect, test, vi} from "vitest";
import SafeReporter from "./safe-reporter";

afterEach(() => vi.restoreAllMocks());

test.each([
  ["timedout", "timedout"],
  ["passed", "passed"],
  ["failed", "failed"],
  ["interrupted", "interrupted"],
  ["private-untrusted-status", "unknown"],
])("runner completion emits only a safe status for %s", (status, expected) => {
  const output = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const reporter = new SafeReporter();
  const result = reporter.onEnd({status, startTime: new Date(), duration: 0} as FullResult);
  expect(output.mock.calls).toEqual([[`AEGIS_E2E end status=${expected}`]]);
  expect(result).toBeUndefined();
});

test("a failed case reports only the sanitizer's spec line", () => {
  const output = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const reporter = new SafeReporter();
  const testCase = {
    id: "t1", location: {line: 339}, parent: {project: () => ({name: "mobile-chromium"})},
  };
  reporter.onBegin(
    {projects: []} as never, {allTests: () => [testCase]} as never,
  );
  reporter.onTestEnd(testCase as never, {
    status: "failed", attachments: [],
    errors: [{message: "withheld", location: {file: "withheld.spec.ts", line: 377, column: 0}}],
  } as never);
  reporter.onTestEnd(testCase as never, {
    status: "failed", attachments: [],
    errors: [{message: "x", location: {file: "/private/path/phase2a.spec.ts", line: 5, column: 1}}],
  } as never);
  expect(output.mock.calls).toEqual([
    ["AEGIS_E2E begin cases=1"],
    ["AEGIS_E2E project=mobile-chromium case=1 line=339 status=failed failed_line=377"],
    ["AEGIS_E2E project=mobile-chromium case=1 line=339 status=failed"],
  ]);
});
