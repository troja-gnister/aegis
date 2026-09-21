import {QueryClient} from "@tanstack/react-query";
import {afterEach, describe, expect, it} from "vitest";
import {purgePrivateBrowserState} from "../auth/cache";
import {createFileNavigation} from "./navigation";

const ROOT_ID = "11111111-1111-4111-8111-111111111111";

function record(index: number) {
  return {
    rootId: ROOT_ID,
    parentId: `${index.toString(16).padStart(8, "0")}-2222-4222-8222-222222222222`,
    filters: {v: 1 as const},
    sort: "name" as const,
    order: "asc" as const,
    cursor: `cursor:${index}`,
    visibleAnchorId: null,
    visibleAnchorOffset: index,
  };
}

const stores = new Set<ReturnType<typeof createFileNavigation>>();

afterEach(() => {
  for (const store of stores) store.dispose();
  stores.clear();
});

describe("file navigation", () => {
  it("keeps one-million-step navigation bounded to the newest 32 metadata records", () => {
    const navigation = createFileNavigation("million-step-namespace");
    stores.add(navigation);
    for (let index = 0; index < 1_000_000; index += 1) {
      navigation.remember(`history-${index}`, record(index));
    }

    expect(navigation.size).toBe(32);
    expect(navigation.recall("history-0")).toBeUndefined();
    expect(navigation.recall("history-999999")).toEqual(record(999_999));
  });

  it("enforces the 8 KiB record bound and true LRU eviction", () => {
    const navigation = createFileNavigation("lru-namespace");
    stores.add(navigation);
    for (let index = 0; index < 32; index += 1) navigation.remember(`history-${index}`, record(index));
    expect(navigation.recall("history-0")).toEqual(record(0));
    navigation.remember("history-32", record(32));
    expect(navigation.recall("history-1")).toBeUndefined();
    expect(() => navigation.remember("oversized", {
      ...record(33),
      filters: {v: 1, prefix: "x".repeat(9 * 1024)},
    })).toThrow("Navigation record exceeds 8192 bytes");
    expect(() => navigation.remember("x".repeat(9 * 1024), record(34)))
      .toThrow("Navigation record exceeds 8192 bytes");
    expect(navigation.size).toBe(32);
  });

  it("clears synchronously on auth purge and unsubscribes on owner disposal", async () => {
    const navigation = createFileNavigation("cleanup-namespace");
    stores.add(navigation);
    navigation.remember("current", record(1));
    const queryClient = new QueryClient();

    const pending = purgePrivateBrowserState(queryClient);
    expect(navigation.size).toBe(0);
    await pending;

    navigation.remember("after-cleanup", record(2));
    navigation.dispose();
    await purgePrivateBrowserState(queryClient);
    expect(navigation.size).toBe(0);
  });
});
