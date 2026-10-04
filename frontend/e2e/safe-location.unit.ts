import {expect, test} from "vitest";
import {specFailureLine} from "./safe-location";

test("only a spec line number survives", () => {
  const stack = [
    "Error: expect(locator).toBeFocused() failed",
    "    at /home/someone/aegis/frontend/e2e/phase2a.spec.ts:377:72",
    "    at /home/someone/aegis/frontend/e2e/auth.ts:12:3",
  ].join("\n");
  expect(specFailureLine(stack)).toBe(377);
});

test.each([
  [undefined],
  ["Error: no frames"],
  ["    at /home/someone/aegis/frontend/e2e/auth.ts:12:3"],
  ["    at /elsewhere/phase2a.spec.ts:9:1"],
])("returns undefined without an e2e spec frame: %s", (stack) => {
  expect(specFailureLine(stack)).toBeUndefined();
});
