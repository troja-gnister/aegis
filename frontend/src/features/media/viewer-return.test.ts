import {InfiniteQueryObserver, QueryClient} from "@tanstack/react-query";
import {http, HttpResponse} from "msw";
import {afterEach, describe, expect, it, vi} from "vitest";
import {server} from "../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {FIXTURE_ROOT_ID} from "../files/test-fixtures";
import {FIRST_TIMELINE_PAGE, timelineQueryOptions} from "./queries";
import {timelineItemBody, timelineItemId, timelinePageBody} from "./test-fixtures";
import {
  forgetViewerReturn,
  isReturnRetained,
  peekViewerReturn,
  releaseReturnRetention,
  rememberViewerReturn,
  retainForReturn,
  RETURN_RETENTION_MS,
  updateViewerReturn,
} from "./viewer-return";

const clients = new Set<QueryClient>();

async function client() {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, NAMESPACE);
  return queryClient;
}

function tick() {
  return new Promise((resolve) => setTimeout(resolve, 10));
}

afterEach(async () => {
  vi.useRealTimers();
  await Promise.all([...clients].map((queryClient) => purgePrivateBrowserState(queryClient)));
  clients.clear();
});

const NAMESPACE = "viewer-return-namespace";

describe("viewer return records", () => {
  it("keeps the opened anchor while recording the entry the viewer shows", () => {
    rememberViewerReturn(NAMESPACE, "opener", {
      entryId: timelineItemId(1),
      openedEntryId: timelineItemId(1),
      anchorOffset: 120,
      timelinePageParam: FIRST_TIMELINE_PAGE,
    });
    const second = {cursor: "page-1", index: 1, previous: FIRST_TIMELINE_PAGE};
    updateViewerReturn(NAMESPACE, "opener", {entryId: timelineItemId(70), timelinePageParam: second});

    expect(peekViewerReturn(NAMESPACE, "opener")).toEqual({
      entryId: timelineItemId(70),
      openedEntryId: timelineItemId(1),
      anchorOffset: 120,
      timelinePageParam: second,
    });
    // Peeking does not consume; forgetting does.
    expect(peekViewerReturn(NAMESPACE, "opener")).not.toBeNull();
    forgetViewerReturn(NAMESPACE, "opener");
    expect(peekViewerReturn(NAMESPACE, "opener")).toBeNull();
  });

  it("creates a record for an opener that left none, scoped to its namespace", () => {
    updateViewerReturn(NAMESPACE, "details", {entryId: timelineItemId(3)});
    expect(peekViewerReturn(NAMESPACE, "details")).toEqual({
      entryId: timelineItemId(3),
      openedEntryId: timelineItemId(3),
      anchorOffset: null,
      timelinePageParam: null,
    });
    expect(peekViewerReturn("another-namespace", "details")).toBeNull();
  });

  it("forgets every record with the private state", async () => {
    updateViewerReturn(NAMESPACE, "purged", {entryId: timelineItemId(5)});
    await purgePrivateBrowserState(new QueryClient());
    expect(peekViewerReturn(NAMESPACE, "purged")).toBeNull();
  });
});

describe("held windows for the viewer's return", () => {
  const input = {namespace: NAMESPACE, rootId: FIXTURE_ROOT_ID, rootEpoch: 4, kind: "all" as const, year: null};

  async function loadedWindow(queryClient: QueryClient) {
    server.use(http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/timeline`, () =>
      HttpResponse.json(timelinePageBody([timelineItemBody(0)]))));
    const options = timelineQueryOptions(input);
    const observer = new InfiniteQueryObserver(queryClient, options);
    const unsubscribe = observer.subscribe(() => undefined);
    await observer.refetch();
    return {options, unsubscribe};
  }

  it("drops an unheld gcTime-0 window as soon as its last observer leaves", async () => {
    const queryClient = await client();
    const {options, unsubscribe} = await loadedWindow(queryClient);
    unsubscribe();
    await tick();
    expect(queryClient.getQueryData(options.queryKey)).toBeUndefined();
  });

  it("holds the window without fetching until the opener releases it", async () => {
    const queryClient = await client();
    const {options, unsubscribe} = await loadedWindow(queryClient);
    retainForReturn(queryClient, options, NAMESPACE, "opener");
    unsubscribe();
    await tick();

    expect(isReturnRetained(NAMESPACE, "opener")).toBe(true);
    expect(queryClient.getQueryData(options.queryKey)).toBeDefined();
    expect(queryClient.getQueryCache().find({queryKey: options.queryKey})?.state.fetchStatus).toBe("idle");

    releaseReturnRetention(NAMESPACE, "opener");
    await tick();
    expect(isReturnRetained(NAMESPACE, "opener")).toBe(false);
    expect(queryClient.getQueryData(options.queryKey)).toBeUndefined();
  });

  it("releases every held window with the private state", async () => {
    const queryClient = await client();
    const {options, unsubscribe} = await loadedWindow(queryClient);
    retainForReturn(queryClient, options, NAMESPACE, "opener");
    unsubscribe();

    await purgePrivateBrowserState(queryClient);

    expect(isReturnRetained(NAMESPACE, "opener")).toBe(false);
    expect(queryClient.getQueryCache().findAll({queryKey: ["media"]})).toHaveLength(0);
  });

  it("holds nothing for a namespace that is no longer current", async () => {
    const queryClient = await client();
    retainForReturn(queryClient, {queryKey: ["media", "other-namespace", "timeline"]}, "other-namespace", "opener");
    expect(isReturnRetained("other-namespace", "opener")).toBe(false);
  });

  it("lets a held window go after the retention time", async () => {
    const queryClient = await client();
    vi.useFakeTimers();
    retainForReturn(queryClient, {queryKey: ["media", NAMESPACE, "timeline", "held"]}, NAMESPACE, "timed");
    expect(isReturnRetained(NAMESPACE, "timed")).toBe(true);
    vi.advanceTimersByTime(RETURN_RETENTION_MS - 1);
    expect(isReturnRetained(NAMESPACE, "timed")).toBe(true);
    vi.advanceTimersByTime(1);
    expect(isReturnRetained(NAMESPACE, "timed")).toBe(false);
  });
});
