import {
  InfiniteQueryObserver,
  QueryClient,
} from "@tanstack/react-query";
import {http, HttpResponse} from "msw";
import {afterEach, describe, expect, it, vi} from "vitest";
import {ApiProblem} from "../../api/problem";
import {server} from "../../test/server";
import {
  activateCacheNamespace,
  capturePrivateState,
  isPrivateStateCurrent,
  purgePrivateBrowserState,
} from "../auth/cache";
import {isSessionAccessOpen} from "../auth/session";
import * as fileApi from "./api";
import {createFileNavigation} from "./navigation";
import {
  directoryQueryOptions,
  entryQueryOptions,
  indexStatusQueryOptions,
} from "./queries";
import type {BrowseInput, DirectoryPage} from "./types";

const ROOT_ID = "11111111-1111-4111-8111-111111111111";
const FOREIGN_ROOT_ID = "99999999-9999-4999-8999-999999999999";
const DIRECTORY_ID = "22222222-2222-4222-8222-222222222222";
const NAMESPACE = "browse-query-namespace";
const clients = new Set<QueryClient>();

const input: BrowseInput = {
  namespace: NAMESPACE,
  rootId: ROOT_ID,
  rootEpoch: 2,
  parentId: DIRECTORY_ID,
  filters: {v: 1},
  sort: "name",
  order: "asc",
};

function entryId(page: number, row: number): string {
  const prefix = (page * 100 + row).toString(16).padStart(8, "0");
  return `${prefix}-1111-4111-8111-111111111111`;
}

function responsePage(
  pageNumber: number,
  rootId = ROOT_ID,
  cursorGeneration = 0,
): DirectoryPage {
  return {
    entries: Array.from({length: 100}, (_, row) => ({
      id: entryId(pageNumber, row),
      rootId,
      displayName: `entry-${pageNumber}-${row}`,
      kind: "file",
      typeHint: null,
      size: "1",
      modifiedNs: null,
      sourceState: "present",
      version: "1",
    })),
    nextCursor: pageNumber < 20 ? `cursor:${cursorGeneration}:${pageNumber + 1}` : null,
    previousCursor: pageNumber > 0 ? `cursor:${cursorGeneration}:${pageNumber - 1}` : null,
    directoryId: DIRECTORY_ID,
    directoryVersion: "1",
    contractVersion: 1,
    indexStatus: {
      state: "ready",
      generation: "1",
      observedEntries: "2100",
      completedDirectories: "1",
      degradedDirectories: "0",
      updatedAt: "2026-09-21T12:00:00+00:00",
      lastCompletedAt: "2026-09-21T11:59:59.123456+00:00",
    },
  };
}

async function createBrowseQueryFixture() {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, NAMESPACE);
  let foreign = false;
  let duplicateFromPage: number | null = null;
  let cursorGeneration = 0;
  server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, ({request}) => {
    const cursor = new URL(request.url).searchParams.get("cursor");
    const pageNumber = cursor === null ? 0 : Number(cursor.split(":").at(-1));
    const page = responsePage(
      pageNumber,
      foreign ? FOREIGN_ROOT_ID : ROOT_ID,
      cursorGeneration,
    );
    if (duplicateFromPage !== null) page.entries[0]!.id = entryId(duplicateFromPage, 0);
    return HttpResponse.json(page);
  }));
  const observer = new InfiniteQueryObserver(queryClient, directoryQueryOptions(input));
  const unsubscribe = observer.subscribe(() => undefined);
  await observer.refetch();
  return {
    async fetchNext() { return observer.fetchNextPage(); },
    async fetchPrevious() { return observer.fetchPreviousPage(); },
    async refetch() { return observer.refetch(); },
    pages: () => observer.getCurrentResult().data?.pages ?? [],
    entryIds: () => (observer.getCurrentResult().data?.pages ?? []).flatMap((page) => page.entries.map((entry) => entry.id)),
    respondWithForeignRoot() { foreign = true; },
    respondWithDuplicateFrom(pageNumber: number | null) { duplicateFromPage = pageNumber; },
    renewCursors() { cursorGeneration += 1; },
    dispose() { unsubscribe(); },
  };
}

afterEach(async () => {
  await Promise.all([...clients].map((client) => purgePrivateBrowserState(client)));
  clients.clear();
});

describe("directoryQueryOptions", () => {
  it("bounds the active directory and rejects foreign-root rows", async () => {
    const setup = await createBrowseQueryFixture();
    try {
      for (let page = 1; page < 12; page += 1) await setup.fetchNext();
      expect(setup.pages()).toHaveLength(5);
      expect(setup.entryIds().length).toBeLessThanOrEqual(500);
      setup.respondWithForeignRoot();
      const result = await setup.fetchNext();
      expect(result.error).toMatchObject({status: 502});
    } finally {
      setup.dispose();
    }
  });

  it("fetches previous pages and rejects duplicates inside the retained window", async () => {
    const setup = await createBrowseQueryFixture();
    try {
      for (let page = 1; page < 7; page += 1) await setup.fetchNext();
      const newestFirstId = setup.pages()[0]!.entries[0]!.id;
      await setup.fetchPrevious();
      expect(setup.pages()[0]!.entries[0]!.id).not.toBe(newestFirstId);
      setup.respondWithDuplicateFrom(5);
      const result = await setup.fetchNext();
      expect(result.error).toMatchObject({status: 502});
    } finally {
      setup.dispose();
    }
  });

  it("refetches retained page parameters without mistaking them for new duplicates", async () => {
    const setup = await createBrowseQueryFixture();
    try {
      await setup.fetchNext();
      await setup.fetchNext();
      setup.renewCursors();
      const result = await setup.refetch();
      expect(result.error).toBeNull();
      expect(setup.pages()).toHaveLength(3);
    } finally {
      setup.dispose();
    }
  });

  it("checks duplicates against only the surviving window on append and prepend eviction", async () => {
    const setup = await createBrowseQueryFixture();
    try {
      for (let page = 1; page < 5; page += 1) await setup.fetchNext();
      setup.respondWithDuplicateFrom(0);
      const appended = await setup.fetchNext();
      expect(appended.error).toBeNull();
      expect(setup.pages()).toHaveLength(5);

      setup.respondWithDuplicateFrom(null);
      await setup.fetchNext();
      setup.respondWithDuplicateFrom(6);
      const prepended = await setup.fetchPrevious();
      expect(prepended.error).toBeNull();
      expect(setup.pages()).toHaveLength(5);
      expect(new Set(setup.entryIds()).size).toBe(setup.entryIds().length);
    } finally {
      setup.dispose();
    }
  });

  it("keys filters and namespaces but never a transient cursor", () => {
    const first = directoryQueryOptions({...input, cursor: "cursor:one"});
    const cursorChanged = directoryQueryOptions({...input, cursor: "cursor:two"});
    const filterChanged = directoryQueryOptions({...input, filters: {v: 1, prefix: "a"}});
    const namespaceChanged = directoryQueryOptions({...input, namespace: "another-namespace"});

    expect(first.queryKey).toEqual(cursorChanged.queryKey);
    expect(first.queryKey).not.toEqual(filterChanged.queryKey);
    expect(first.queryKey).not.toEqual(namespaceChanged.queryKey);
    expect(first.gcTime).toBe(0);
    expect(first.maxPages).toBe(5);
  });

  it("discards inactive queries across repeated filter and namespace observer changes", async () => {
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    const observer = new InfiniteQueryObserver(queryClient, {
      ...directoryQueryOptions(input),
      enabled: false,
    });
    const unsubscribe = observer.subscribe(() => undefined);

    try {
      for (let change = 0; change < 100; change += 1) {
        observer.setOptions({
          ...directoryQueryOptions({
            ...input,
            namespace: `namespace-${change % 3}`,
            filters: {v: 1, prefix: `prefix-${change}`},
          }),
          enabled: false,
        });
        await new Promise((resolve) => window.setTimeout(resolve, 0));
        expect(queryClient.getQueryCache().getAll().length).toBeLessThanOrEqual(1);
      }
    } finally {
      unsubscribe();
    }
  });

  it("disables inactive retention for details and status query options", () => {
    const entry = entryQueryOptions(NAMESPACE, entryId(0, 0));
    const indexStatus = indexStatusQueryOptions(NAMESPACE, ROOT_ID, 2);

    expect(entry.queryKey).toEqual(["files", NAMESPACE, "entry", entryId(0, 0)]);
    expect(indexStatus.queryKey).toEqual(["files", NAMESPACE, "index-status", ROOT_ID, 2]);
    expect(entry.gcTime).toBe(0);
    expect(indexStatus.gcTime).toBe(0);
  });

  it("does not execute retained details or status options after their namespace is replaced", async () => {
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    await activateCacheNamespace(queryClient, NAMESPACE);
    const retainedEntry = entryQueryOptions(NAMESPACE, entryId(0, 0));
    const retainedStatus = indexStatusQueryOptions(NAMESPACE, ROOT_ID, 2);
    let requests = 0;
    server.use(
      http.get(`/api/v1/entries/${entryId(0, 0)}`, () => {
        requests += 1;
        return HttpResponse.json({});
      }),
      http.get(`/api/v1/roots/${ROOT_ID}/index-status`, () => {
        requests += 1;
        return HttpResponse.json({});
      }),
    );
    await activateCacheNamespace(queryClient, "replacement-namespace");

    await expect(queryClient.fetchQuery(retainedEntry)).rejects.toMatchObject({status: 0});
    await expect(queryClient.fetchQuery(retainedStatus)).rejects.toMatchObject({status: 0});
    expect(requests).toBe(0);
    expect(queryClient.getQueryData(retainedEntry.queryKey)).toBeUndefined();
    expect(queryClient.getQueryData(retainedStatus.queryKey)).toBeUndefined();
  });

  it("rechecks initiating ownership before an obsolete 401 can affect a newer account", async () => {
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    const replacementClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    clients.add(replacementClient);
    await activateCacheNamespace(queryClient, NAMESPACE);
    let transitioned = () => {};
    const transitionDone = new Promise<void>((resolve) => { transitioned = resolve; });
    vi.spyOn(fileApi, "fetchEntry").mockImplementation(async () => {
      queueMicrotask(() => {
        void activateCacheNamespace(replacementClient, "replacement-namespace").then(transitioned);
      });
      throw new ApiProblem({
        type: "authentication_required",
        title: "Authentication required",
        status: 401,
      });
    });

    const pending = queryClient.fetchQuery(entryQueryOptions(NAMESPACE, entryId(0, 0)));
    await transitionDone;
    await expect(pending).rejects.toBeDefined();

    expect(isSessionAccessOpen()).toBe(true);
    expect(isPrivateStateCurrent(capturePrivateState(), "replacement-namespace")).toBe(true);
  });

  it("rechecks initiating ownership before an obsolete 404 can clear renewed root state", async () => {
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    const replacementClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    clients.add(replacementClient);
    await activateCacheNamespace(queryClient, NAMESPACE);
    let transitioned = () => {};
    const transitionDone = new Promise<void>((resolve) => { transitioned = resolve; });
    let replacementNavigation: ReturnType<typeof createFileNavigation> | undefined;
    vi.spyOn(fileApi, "fetchIndexStatus").mockImplementation(async () => {
      queueMicrotask(() => {
        void (async () => {
          await purgePrivateBrowserState(replacementClient);
          await activateCacheNamespace(replacementClient, NAMESPACE);
          replacementNavigation = createFileNavigation(NAMESPACE);
          replacementNavigation.remember("renewed", {
            rootId: ROOT_ID, parentId: DIRECTORY_ID, filters: {v: 1}, sort: "name",
            order: "asc", cursor: null, visibleAnchorId: null, visibleAnchorOffset: 0,
          });
          replacementClient.setQueryData(["roots", NAMESPACE], {roots: []});
          transitioned();
        })();
      });
      throw new ApiProblem({
        type: "catalog_not_found",
        title: "Catalog item not found",
        status: 404,
      });
    });

    try {
      const pending = queryClient.fetchQuery(indexStatusQueryOptions(NAMESPACE, ROOT_ID, 2));
      await transitionDone;
      await expect(pending).rejects.toMatchObject({status: 0});

      expect(replacementNavigation?.size).toBe(1);
      expect(replacementClient.getQueryState(["roots", NAMESPACE])?.isInvalidated).toBe(false);
    } finally {
      replacementNavigation?.dispose();
    }
  });

  it("does not publish a response parsed after an account switch", async () => {
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    await activateCacheNamespace(queryClient, NAMESPACE);
    let release = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, async () => {
      await gate;
      return HttpResponse.json(responsePage(0));
    }));
    const pending = queryClient.fetchInfiniteQuery(directoryQueryOptions(input));
    await activateCacheNamespace(queryClient, "replacement-namespace");
    release();

    await expect(pending).rejects.toBeDefined();
    expect(queryClient.getQueryData(directoryQueryOptions(input).queryKey)).toBeUndefined();
  });

  it("aborts in-flight requests without publishing their page", async () => {
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    await activateCacheNamespace(queryClient, NAMESPACE);
    let requested = false;
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, async ({request}) => {
      requested = true;
      await new Promise<void>((resolve) => request.signal.addEventListener("abort", () => resolve(), {once: true}));
      return HttpResponse.json(responsePage(0));
    }));
    const pending = queryClient.fetchInfiniteQuery(directoryQueryOptions(input));
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    if (!requested) {
      await expect(pending).rejects.toBeDefined();
      expect(requested).toBe(true);
      return;
    }
    await queryClient.cancelQueries({queryKey: directoryQueryOptions(input).queryKey});
    await expect(pending).rejects.toBeDefined();
    expect(queryClient.getQueryData(directoryQueryOptions(input).queryKey)).toBeUndefined();
  });

  it("closes access and synchronously purges private state on catalog 401", async () => {
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    await activateCacheNamespace(queryClient, NAMESPACE);
    const navigation = createFileNavigation(NAMESPACE);
    navigation.remember("root", {
      rootId: ROOT_ID, parentId: DIRECTORY_ID, filters: {v: 1}, sort: "name",
      order: "asc", cursor: null, visibleAnchorId: null, visibleAnchorOffset: 0,
    });
    queryClient.setQueryData(["private", NAMESPACE], "secret");
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () =>
      HttpResponse.json({type: "authentication_required", title: "Authentication required"}, {status: 401}),
    ));

    await expect(queryClient.fetchInfiniteQuery(directoryQueryOptions(input))).rejects.toBeDefined();
    expect(isSessionAccessOpen()).toBe(false);
    expect(navigation.size).toBe(0);
    expect(queryClient.getQueryData(["private", NAMESPACE])).toBeUndefined();
    navigation.dispose();
  });

  it("clears the missing root navigation and invalidates authorized roots on 404", async () => {
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    await activateCacheNamespace(queryClient, NAMESPACE);
    const navigation = createFileNavigation(NAMESPACE);
    navigation.remember("missing", {
      rootId: ROOT_ID, parentId: DIRECTORY_ID, filters: {v: 1}, sort: "name",
      order: "asc", cursor: null, visibleAnchorId: null, visibleAnchorOffset: 0,
    });
    navigation.remember("other", {
      rootId: FOREIGN_ROOT_ID, parentId: null, filters: {v: 1}, sort: "name",
      order: "asc", cursor: null, visibleAnchorId: null, visibleAnchorOffset: 0,
    });
    queryClient.setQueryData(["roots", NAMESPACE], {roots: []});
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () =>
      HttpResponse.json({type: "catalog_not_found", title: "Catalog item not found"}, {status: 404}),
    ));

    await expect(queryClient.fetchInfiniteQuery(directoryQueryOptions(input))).rejects.toMatchObject({status: 404});
    expect(navigation.size).toBe(1);
    expect(queryClient.getQueryState(["roots", NAMESPACE])?.isInvalidated).toBe(true);
    navigation.dispose();
  });
});
