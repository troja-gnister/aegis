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
