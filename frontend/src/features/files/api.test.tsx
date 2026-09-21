import {http, HttpResponse} from "msw";
import {afterEach, beforeEach, describe, expect, it} from "vitest";
import {server} from "../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {QueryClient} from "@tanstack/react-query";
import {
  fetchDirectory,
  fetchEntry,
  fetchIndexStatus,
  requestScan,
} from "./api";
import type {BrowseInput, DirectoryPage} from "./types";

const ROOT_ID = "11111111-1111-4111-8111-111111111111";
const DIRECTORY_ID = "22222222-2222-4222-8222-222222222222";
const ENTRY_ID = "33333333-3333-4333-8333-333333333333";
const NAMESPACE = "browser-api-namespace";

const input: BrowseInput = {
  namespace: NAMESPACE,
  rootId: ROOT_ID,
  rootEpoch: 7,
  parentId: DIRECTORY_ID,
  filters: {v: 1, type: ["jpg", "__unknown__"]},
  sort: "name",
  order: "asc",
};

function status() {
  return {
    state: "ready" as const,
    generation: "8",
    observedEntries: "10",
    completedDirectories: "2",
    degradedDirectories: "0",
    updatedAt: "2026-09-21T12:34:56.123456+00:00",
    lastCompletedAt: null,
  };
}

function summary(id = ENTRY_ID) {
  return {
    id,
    rootId: ROOT_ID,
    displayName: "photo.jpg",
    kind: "file" as const,
    typeHint: "jpg",
    size: "18446744073709551615",
    modifiedNs: "-9223372036854775808",
    sourceState: "present" as const,
    version: "9223372036854775807",
  };
}

function page(): DirectoryPage {
  return {
    entries: [summary()],
    nextCursor: "signed-looking:next-token",
    previousCursor: null,
    directoryId: DIRECTORY_ID,
    directoryVersion: "4",
    contractVersion: 1,
    indexStatus: status(),
  };
}

const clients = new Set<QueryClient>();

beforeEach(async () => {
  const client = new QueryClient();
  clients.add(client);
  await activateCacheNamespace(client, NAMESPACE);
});

describe("file API guards", () => {
  it("serializes only the bounded catalog query contract and accepts valid pages", async () => {
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, ({request}) => {
      const url = new URL(request.url);
      expect([...url.searchParams.keys()].sort()).toEqual(
        ["filters", "limit", "order", "parent", "sort"],
      );
      expect(url.searchParams.get("limit")).toBe("100");
      expect(JSON.parse(url.searchParams.get("filters") ?? "")).toEqual(input.filters);
      return HttpResponse.json(page());
    }));

    await expect(fetchDirectory(input, new AbortController().signal)).resolves.toEqual(page());
  });

  it.each([
    ["foreign root row", (value: DirectoryPage) => { value.entries[0]!.rootId = "44444444-4444-4444-8444-444444444444"; }],
    ["duplicate IDs", (value: DirectoryPage) => { value.entries.push({...value.entries[0]!}); }],
    ["directory mismatch", (value: DirectoryPage) => { value.directoryId = ENTRY_ID; }],
    ["unsafe numeric encoding", (value: DirectoryPage) => { (value.entries[0] as unknown as {size: number}).size = Number.MAX_SAFE_INTEGER + 1; }],
    ["out-of-domain decimal", (value: DirectoryPage) => { value.entries[0]!.version = "9223372036854775808"; }],
    ["invalid nullable field", (value: DirectoryPage) => { (value.entries[0] as unknown as {modifiedNs: undefined}).modifiedNs = undefined; }],
    ["invalid cursor", (value: DirectoryPage) => { value.nextCursor = ".compressed-is-not-accepted"; }],
    ["private extra field", (value: DirectoryPage) => { (value.entries[0] as unknown as {sourcePath: string}).sourcePath = "/private/archive"; }],
  ])("rejects %s in a successful directory body", async (_name, mutate) => {
    const body = page();
    mutate(body);
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () => HttpResponse.json(body)));

    await expect(fetchDirectory(input, new AbortController().signal)).rejects.toMatchObject({
      status: 502,
    });
  });

  it("rejects oversized page arrays and success bodies before publication", async () => {
    const body = page();
    body.entries = Array.from({length: 251}, (_, index) =>
      summary(`${index.toString(16).padStart(8, "0")}-1111-4111-8111-111111111111`),
    );
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () => HttpResponse.json(body)));
    await expect(fetchDirectory(input, new AbortController().signal)).rejects.toMatchObject({status: 502});

    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () =>
      HttpResponse.json({...page(), padding: "x".repeat(1024 * 1024)}),
    ));
    await expect(fetchDirectory(input, new AbortController().signal)).rejects.toMatchObject({status: 502});
  });

  it("rejects malformed JSON and an account switch while a body is being parsed", async () => {
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () =>
      new HttpResponse("{", {headers: {"Content-Type": "application/json"}}),
    ));
    await expect(fetchDirectory(input, new AbortController().signal)).rejects.toMatchObject({status: 502});

    const encoded = new TextEncoder().encode(JSON.stringify(page()));
    let started = () => {};
    const firstChunk = new Promise<void>((resolve) => { started = resolve; });
    let release = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () => new HttpResponse(
      new ReadableStream({
        start(controller) {
          controller.enqueue(encoded.slice(0, Math.floor(encoded.length / 2)));
          started();
          void gate.then(() => {
            controller.enqueue(encoded.slice(Math.floor(encoded.length / 2)));
            controller.close();
          });
        },
      }),
      {headers: {"Content-Type": "application/json"}},
    )));
    const pending = fetchDirectory(input, new AbortController().signal);
    await firstChunk;
    const switchClient = new QueryClient();
    clients.add(switchClient);
    await activateCacheNamespace(switchClient, "switched-account");
    release();

    await expect(pending).rejects.toMatchObject({status: 0});
  });

  it("ignores an obsolete 401 after a newer account owns private state", async () => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, async () => {
      await gate;
      return HttpResponse.json(
        {type: "authentication_required", title: "Authentication required"},
        {status: 401},
      );
    }));
    const pending = fetchDirectory(input, new AbortController().signal);
    const switchClient = new QueryClient();
    clients.add(switchClient);
    await activateCacheNamespace(switchClient, "new-owner-namespace");
    release();

    await expect(pending).rejects.toMatchObject({status: 0});
  });

  it.each([
    ["a filesystem path", {...input, path: "/private/archive"}],
    ["an out-of-domain modified time", {...input, filters: {v: 1, modified: {from: "2263-01-01T00:00:00Z"}}}],
    ["an inverted modified range", {...input, filters: {v: 1, modified: {
      from: "2026-09-22T00:00:00Z", before: "2026-09-21T00:00:00Z",
    }}}],
  ])("rejects browse input containing %s before issuing a request", async (_name, malformed) => {
    let requests = 0;
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () => {
      requests += 1;
      return HttpResponse.json(page());
    }));

    await expect(fetchDirectory(malformed as BrowseInput, new AbortController().signal)).rejects.toMatchObject({status: 502});
    expect(requests).toBe(0);
  });

  it("validates details, ancestors, index status, and scan responses", async () => {
    server.use(
      http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json({
        ...summary(),
        parentId: DIRECTORY_ID,
        ancestors: [{id: DIRECTORY_ID, displayName: ""}],
        ancestorsTruncated: false,
      })),
      http.get(`/api/v1/roots/${ROOT_ID}/index-status`, () => HttpResponse.json(status())),
      http.post(`/api/v1/roots/${ROOT_ID}/scans`, ({request}) => {
        expect(request.headers.get("X-Request-ID")).toBe("request_12345678");
        expect(request.headers.get("X-CSRFToken")).toBe("csrf-token");
        return HttpResponse.json({scanId: ENTRY_ID}, {status: 202});
      }),
    );

    await expect(fetchEntry(ENTRY_ID, new AbortController().signal)).resolves.toMatchObject({
      id: ENTRY_ID,
      ancestorsTruncated: false,
    });
    await expect(fetchIndexStatus(ROOT_ID, new AbortController().signal)).resolves.toEqual(status());
    await expect(requestScan(ROOT_ID, "request_12345678", "csrf-token")).resolves.toEqual({
      scanId: ENTRY_ID,
    });
  });

  it("rejects more than 64 ancestors and malformed scan/status bodies", async () => {
    server.use(
      http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json({
        ...summary(),
        parentId: DIRECTORY_ID,
        ancestors: Array.from({length: 65}, () => ({id: DIRECTORY_ID, displayName: "A"})),
        ancestorsTruncated: true,
      })),
      http.get(`/api/v1/roots/${ROOT_ID}/index-status`, () => HttpResponse.json({...status(), worker: "private"})),
      http.post(`/api/v1/roots/${ROOT_ID}/scans`, () => HttpResponse.json({scanId: null}, {status: 202})),
    );

    await expect(fetchEntry(ENTRY_ID, new AbortController().signal)).rejects.toMatchObject({status: 502});
    await expect(fetchIndexStatus(ROOT_ID, new AbortController().signal)).rejects.toMatchObject({status: 502});
    await expect(requestScan(ROOT_ID, "request_12345678", "csrf-token")).rejects.toMatchObject({status: 502});
  });

  it("rejects valid details for an entry other than the requested ID", async () => {
    const otherId = "44444444-4444-4444-8444-444444444444";
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json({
      ...summary(otherId),
      parentId: DIRECTORY_ID,
      ancestors: [],
      ancestorsTruncated: false,
    })));

    await expect(fetchEntry(ENTRY_ID, new AbortController().signal)).rejects.toMatchObject({
      status: 502,
    });
  });
});

afterEach(async () => {
  await Promise.all([...clients].map((client) => purgePrivateBrowserState(client)));
  clients.clear();
});
