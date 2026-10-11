import {InfiniteQueryObserver, QueryClient} from "@tanstack/react-query";
import {http, HttpResponse} from "msw";
import {afterEach, describe, expect, it} from "vitest";
import {ApiProblem} from "../../api/problem";
import {server} from "../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {isSessionAccessOpen} from "../auth/session";
import {FIXTURE_ROOT_ID} from "../files/test-fixtures";
import {
  FIRST_TIMELINE_PAGE,
  mediaQueryKeys,
  TIMELINE_CHAIN_LINKS,
  TIMELINE_MAX_PAGES,
  timelineItems,
  timelinePageParamFor,
  timelineQueryOptions,
  timelineWindowTruncated,
  type TimelineData,
  type TimelinePageParam,
} from "./queries";
import {fetchTimeline, TIMELINE_PAGE_LIMIT, type TimelineInput} from "./timeline-api";
import {timelineItemBody, timelineItemId, timelinePageBody} from "./test-fixtures";

const NAMESPACE = "timeline-namespace";
const FOREIGN_ROOT_ID = "99999999-9999-4999-8999-999999999999";
const TIMELINE_PATH = `/api/v1/roots/${FIXTURE_ROOT_ID}/timeline`;
const clients = new Set<QueryClient>();

const input: TimelineInput = {namespace: NAMESPACE, rootId: FIXTURE_ROOT_ID, rootEpoch: 4, kind: "all", year: null};

async function client() {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, NAMESPACE);
  return queryClient;
}

function signal() {
  return new AbortController().signal;
}

function items(start: number, length: number) {
  return Array.from({length}, (_, offset) => timelineItemBody(start + offset));
}

afterEach(async () => {
  await Promise.all([...clients].map((queryClient) => purgePrivateBrowserState(queryClient)));
  clients.clear();
});

describe("timeline API", () => {
  it("requests a bounded keyset page with the filter and validates items", async () => {
    await client();
    let requested = null as URL | null;
    server.use(http.get(TIMELINE_PATH, ({request}) => {
      requested = new URL(request.url);
      return HttpResponse.json(timelinePageBody(items(0, 3), "cursor-2"));
    }));

    const page = await fetchTimeline({...input, kind: "video", year: 2026}, "cursor-1", signal()).catch((error) => error);
    // The items are photos, so a video filter's answer is a contract breach.
    expect(page).toBeInstanceOf(ApiProblem);
    expect(requested!.searchParams.get("kind")).toBe("video");
    expect(requested!.searchParams.get("year")).toBe("2026");
    expect(requested!.searchParams.get("cursor")).toBe("cursor-1");
    expect(requested!.searchParams.get("limit")).toBe(String(TIMELINE_PAGE_LIMIT));

    const accepted = await fetchTimeline(input, null, signal());
    expect(requested!.searchParams.get("kind")).toBe("all");
    expect(requested!.searchParams.has("year")).toBe(false);
    expect(requested!.searchParams.has("cursor")).toBe(false);
    expect(accepted.items.map((item) => item.id)).toEqual([0, 1, 2].map(timelineItemId));
    expect(accepted.items[0]).toMatchObject({parentId: expect.any(String), media: {kind: "photo"}});
    expect(accepted.nextCursor).toBe("cursor-2");
  });

  it.each([
    ["a non-media item", () => ({...timelineItemBody(0), media: null})],
    ["a directory", () => ({...timelineItemBody(0), kind: "directory", typeHint: null})],
    ["a foreign root", () => timelineItemBody(0, {rootId: FOREIGN_ROOT_ID})],
    ["a missing parent", () => {
      const body = timelineItemBody(0);
      delete body.parentId;
      return body;
    }],
    ["an extra key", () => ({...timelineItemBody(0), path: "/private"})],
    ["a cross-origin thumbnail", () => timelineItemBody(0, {media: {thumbnailUrl: "https://elsewhere.test/x.webp"}})],
  ])("rejects %s with a 502", async (_name, item) => {
    await client();
    server.use(http.get(TIMELINE_PATH, () => HttpResponse.json(timelinePageBody([item()]))));
    await expect(fetchTimeline(input, null, signal())).rejects.toMatchObject({status: 502});
  });

  it("rejects duplicate items, oversized pages and unknown contract versions", async () => {
    await client();
    const bodies = [
      timelinePageBody([timelineItemBody(0), timelineItemBody(0)]),
      timelinePageBody(items(0, 121)),
      {...timelinePageBody(items(0, 1)), contractVersion: 2},
      {...timelinePageBody(items(0, 1)), previousCursor: null},
    ];
    for (const body of bodies) {
      server.use(http.get(TIMELINE_PATH, () => HttpResponse.json(body)));
      await expect(fetchTimeline(input, null, signal())).rejects.toMatchObject({status: 502});
    }
  });
});

describe("timeline queries", () => {
  it("keys the timeline by namespace, root, epoch and filter without inactive retention", () => {
    const options = timelineQueryOptions(input);
    expect(options.queryKey).toEqual(["media", NAMESPACE, "timeline", FIXTURE_ROOT_ID, 4, "all", null]);
    expect(options.queryKey).toEqual(mediaQueryKeys.timeline(NAMESPACE, FIXTURE_ROOT_ID, 4, "all", null));
    expect(timelineQueryOptions({...input, rootEpoch: 5}).queryKey).not.toEqual(options.queryKey);
    expect(timelineQueryOptions({...input, kind: "photo"}).queryKey).not.toEqual(options.queryKey);
    expect(timelineQueryOptions({...input, year: 2026}).queryKey).not.toEqual(options.queryKey);
    expect(timelineQueryOptions({...input, namespace: "other"}).queryKey).not.toEqual(options.queryKey);
    expect(options.maxPages).toBe(TIMELINE_MAX_PAGES);
    expect(TIMELINE_MAX_PAGES).toBe(5);
    expect(options.gcTime).toBe(0);
    expect(options.retry).toBe(false);
  });

  it("links page parameters so pages dropped from the front can be fetched again", () => {
    const options = timelineQueryOptions(input);
    const page = {items: [], nextCursor: "cursor-1", contractVersion: 1 as const};
    const second = options.getNextPageParam(page, [page], FIRST_TIMELINE_PAGE, [FIRST_TIMELINE_PAGE]) as TimelinePageParam;
    expect(second).toEqual({cursor: "cursor-1", index: 1, previous: FIRST_TIMELINE_PAGE});
    expect(options.getPreviousPageParam?.(page, [page], second, [second])).toBe(FIRST_TIMELINE_PAGE);
    expect(options.getPreviousPageParam?.(page, [page], FIRST_TIMELINE_PAGE, [FIRST_TIMELINE_PAGE])).toBeUndefined();
    expect(options.getNextPageParam({...page, nextCursor: null}, [page], second, [second])).toBeUndefined();
  });

  it("caps the chain of predecessors and reports the truncated window", () => {
    const options = timelineQueryOptions(input);
    let param = FIRST_TIMELINE_PAGE;
    for (let index = 1; index <= 40; index += 1) {
      const page = {items: [], nextCursor: `cursor-${index}`, contractVersion: 1 as const};
      param = options.getNextPageParam(page, [page], param, [param]) as TimelinePageParam;
    }
    let links = 0;
    for (let link = param.previous; link !== null; link = link.previous) links += 1;
    expect(param.index).toBe(40);
    expect(links).toBe(TIMELINE_CHAIN_LINKS);

    const page = {items: [], nextCursor: null, contractVersion: 1 as const};
    let oldest = param;
    while (oldest.previous !== null) oldest = oldest.previous;
    expect(oldest.index).toBeGreaterThan(0);
    const data: TimelineData = {pages: [page], pageParams: [oldest]};
    expect(timelineWindowTruncated(data, false)).toBe(true);
    expect(timelineWindowTruncated({pages: [page], pageParams: [FIRST_TIMELINE_PAGE]}, false)).toBe(false);
    expect(timelineWindowTruncated(data, true)).toBe(false);
  });

  it("holds at most five pages and re-fetches a dropped first page with its cursor", async () => {
    const queryClient = await client();
    const requestedCursors: (string | null)[] = [];
    server.use(http.get(TIMELINE_PATH, ({request}) => {
      const cursor = new URL(request.url).searchParams.get("cursor");
      requestedCursors.push(cursor);
      const page = cursor === null ? 0 : Number(cursor.slice("page-".length));
      return HttpResponse.json(timelinePageBody(items(page * 2, 2), page < 9 ? `page-${page + 1}` : null));
    }));
    const observer = new InfiniteQueryObserver(queryClient, timelineQueryOptions(input));
    const unsubscribe = observer.subscribe(() => undefined);
    try {
      await observer.refetch();
      for (let page = 1; page <= 5; page += 1) await observer.fetchNextPage();
      const data = observer.getCurrentResult().data as TimelineData;
      expect(data.pages).toHaveLength(5);
      expect(timelineItems(data)[0]!.id).toBe(timelineItemId(2));
      expect(observer.getCurrentResult().hasPreviousPage).toBe(true);
      expect(timelinePageParamFor(data, timelineItemId(4))?.cursor).toBe("page-2");

      await observer.fetchPreviousPage();
      const restored = observer.getCurrentResult().data as TimelineData;
      expect(timelineItems(restored)[0]!.id).toBe(timelineItemId(0));
      expect(restored.pages).toHaveLength(5);
      expect(requestedCursors.at(-1)).toBeNull();
      expect(observer.getCurrentResult().hasPreviousPage).toBe(false);
    } finally {
      unsubscribe();
    }
  });

  it("closes access and purges private state on a timeline 401", async () => {
    const queryClient = await client();
    queryClient.setQueryData(["media", NAMESPACE, "details", "retained"], "private");
    server.use(http.get(TIMELINE_PATH, () =>
      HttpResponse.json({type: "authentication_required", title: "Authentication required"}, {status: 401})));

    await expect(queryClient.fetchInfiniteQuery(timelineQueryOptions(input))).rejects.toBeDefined();

    expect(isSessionAccessOpen()).toBe(false);
    expect(queryClient.getQueryCache().findAll({queryKey: ["media"]})).toHaveLength(0);
  });

  it("does not run retained timeline options after their namespace is replaced", async () => {
    const queryClient = await client();
    const retained = timelineQueryOptions(input);
    let requests = 0;
    server.use(http.get(TIMELINE_PATH, () => {
      requests += 1;
      return HttpResponse.json(timelinePageBody(items(0, 1)));
    }));
    await activateCacheNamespace(queryClient, "replacement-namespace");

    await expect(queryClient.fetchInfiniteQuery(retained)).rejects.toMatchObject({status: 0});
    expect(requests).toBe(0);
  });

  it("discards a timeline page that completes after logout", async () => {
    const queryClient = await client();
    let release = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    let started = () => {};
    const requestStarted = new Promise<void>((resolve) => { started = resolve; });
    server.use(http.get(TIMELINE_PATH, async () => {
      started();
      await held;
      return HttpResponse.json(timelinePageBody(items(0, 2)));
    }));
    const pending = queryClient.fetchInfiniteQuery(timelineQueryOptions(input));
    await requestStarted;
    await purgePrivateBrowserState(queryClient);
    release();

    await expect(pending).rejects.toBeDefined();
    expect(queryClient.getQueryData(timelineQueryOptions(input).queryKey)).toBeUndefined();
  });
});
