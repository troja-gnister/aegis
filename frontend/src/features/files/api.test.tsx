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
import {entryMediaFixture, thumbnailUrlFor} from "../media/test-fixtures";
import {emptyFilterDraft, filtersFromDraft} from "./filter-state";
import {setTestTimeZone} from "./test-fixtures";
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
  scope: "directory",
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
    media: null,
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

  it("requests a root-wide page without a parent and keeps each entry's folder", async () => {
    const located = {...summary(), parentId: DIRECTORY_ID, parentName: "DCIM"};
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, ({request}) => {
      const url = new URL(request.url);
      expect([...url.searchParams.keys()].sort()).toEqual(["filters", "limit", "order", "scope", "sort"]);
      expect(url.searchParams.get("scope")).toBe("root");
      return HttpResponse.json({...page(), directoryId: ENTRY_ID, entries: [located]});
    }));

    const result = await fetchDirectory({...input, parentId: null, scope: "root"}, new AbortController().signal);
    expect(result.entries[0]).toEqual({...summary(), location: {parentId: DIRECTORY_ID, parentName: "DCIM"}});
  });

  it.each([
    ["a missing folder", (entry: Record<string, unknown>) => { delete entry.parentName; }],
    ["a malformed folder ID", (entry: Record<string, unknown>) => { entry.parentId = "../folder"; }],
    ["a non-text folder name", (entry: Record<string, unknown>) => { entry.parentName = 7; }],
    ["a private extra field", (entry: Record<string, unknown>) => { entry.parentPath = "/private"; }],
  ])("rejects %s in a root-wide entry", async (_name, mutate) => {
    const located: Record<string, unknown> = {...summary(), parentId: DIRECTORY_ID, parentName: "DCIM"};
    mutate(located);
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () =>
      HttpResponse.json({...page(), entries: [located]})));

    await expect(fetchDirectory({...input, parentId: null, scope: "root"}, new AbortController().signal))
      .rejects.toMatchObject({status: 502});
  });

  it("refuses a root-wide request that names a parent before sending it", async () => {
    let requests = 0;
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () => {
      requests += 1;
      return HttpResponse.json(page());
    }));
    await expect(fetchDirectory({...input, scope: "root"}, new AbortController().signal)).rejects.toBeDefined();
    await expect(fetchDirectory({...input, scope: "everywhere" as "root"}, new AbortController().signal))
      .rejects.toBeDefined();
    expect(requests).toBe(0);
  });

  it("accepts a media summary with a ready, same-origin thumbnail for the same entry", async () => {
    const body = page();
    body.entries[0]!.media = entryMediaFixture(ENTRY_ID);
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () => HttpResponse.json(body)));

    const result = await fetchDirectory(input, new AbortController().signal);
    expect(result.entries[0]!.media).toEqual(entryMediaFixture(ENTRY_ID));
  });

  it("nulls a malformed optional media dimension instead of failing the page", async () => {
    const body = page();
    (body.entries[0]!.media as unknown) = {...entryMediaFixture(ENTRY_ID), width: "wide", durationMs: -5};
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () => HttpResponse.json(body)));

    const result = await fetchDirectory(input, new AbortController().signal);
    expect(result.entries[0]!.media).toMatchObject({width: null, height: 3024, durationMs: null});
    expect(result.entries[0]!.media?.thumbnailUrl).toBe(thumbnailUrlFor(ENTRY_ID));
  });

  it.each([
    ["a thumbnail URL without the fingerprint parameter", (value: DirectoryPage) => {
      value.entries[0]!.media = entryMediaFixture(ENTRY_ID, {
        thumbnailUrl: `/media/v1/derivatives/${ENTRY_ID}/thumbnail?n=x&e=4`,
      });
    }],
    ["a summary without the media field", (value: DirectoryPage) => {
      delete (value.entries[0] as Partial<DirectoryPage["entries"][number]>).media;
    }],
    ["a thumbnail URL before the thumbnail is ready", (value: DirectoryPage) => {
      value.entries[0]!.media = entryMediaFixture(ENTRY_ID, {thumbnail: "pending"});
    }],
    ["another entry's thumbnail URL", (value: DirectoryPage) => {
      value.entries[0]!.media = entryMediaFixture(ENTRY_ID, {
        thumbnailUrl: thumbnailUrlFor("44444444-4444-4444-8444-444444444444"),
      });
    }],
    ["a cross-origin thumbnail URL", (value: DirectoryPage) => {
      value.entries[0]!.media = entryMediaFixture(ENTRY_ID, {
        thumbnailUrl: `https://elsewhere.test${thumbnailUrlFor(ENTRY_ID)}`,
      });
    }],
    ["a blob thumbnail URL", (value: DirectoryPage) => {
      value.entries[0]!.media = entryMediaFixture(ENTRY_ID, {thumbnailUrl: "blob:http://localhost/private"});
    }],
    ["an unknown thumbnail state", (value: DirectoryPage) => {
      (value.entries[0]!.media as unknown) = {...entryMediaFixture(ENTRY_ID), thumbnail: "building"};
    }],
    ["a missing sort date", (value: DirectoryPage) => {
      (value.entries[0]!.media as unknown) = {...entryMediaFixture(ENTRY_ID), sortDate: null};
    }],
    ["an extra media field", (value: DirectoryPage) => {
      (value.entries[0]!.media as unknown) = {...entryMediaFixture(ENTRY_ID), gps: "51.5,-0.1"};
    }],
  ])("rejects %s", async (_name, mutate) => {
    const body = page();
    mutate(body);
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, () => HttpResponse.json(body)));
    await expect(fetchDirectory(input, new AbortController().signal)).rejects.toMatchObject({status: 502});
  });

  it("accepts every filter shape the filter panel can produce under the shared bounds", async () => {
    const restoreTimeZone = setTestTimeZone("America/New_York");
    const drafts = [
      {...emptyFilterDraft(), kind: ["directory", "file", "symlink", "special"] as const},
      {...emptyFilterDraft(), type: ["jpg", "__unknown__"], extraTypes: "unknown, " +
        Array.from({length: 29}, (_, index) => `t${index}`).join(",")},
      {...emptyFilterDraft(), availability: ["present", "missing", "inaccessible", "unsupported"] as const},
      {...emptyFilterDraft(), prefix: "é".repeat(1024)},
      {...emptyFilterDraft(), sizeMin: "0", sizeMax: "18446744073709551615"},
      {...emptyFilterDraft(), sizeUnknown: true, modifiedUnknown: true},
      {...emptyFilterDraft(), modifiedFrom: "2026-03-08", modifiedTo: "2026-11-01"},
      {...emptyFilterDraft(), modifiedTo: "2261-12-31"},
    ];
    const sent: unknown[] = [];
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, ({request}) => {
      sent.push(JSON.parse(new URL(request.url).searchParams.get("filters")!));
      return HttpResponse.json(page());
    }));
    try {
      for (const draft of drafts) {
        const result = filtersFromDraft({...draft, kind: [...draft.kind], availability: [...draft.availability]});
        if (!result.ok) throw new Error(JSON.stringify(result.errors));
        await expect(fetchDirectory({...input, filters: result.filters}, new AbortController().signal))
          .resolves.toEqual(page());
      }
    } finally {
      restoreTimeZone();
    }
    expect(sent).toHaveLength(drafts.length);
    expect(sent[1]).toMatchObject({type: expect.arrayContaining(["__unknown__", "unknown"])});
  });

  it("sends a name search with the other filters and refuses a malformed one before sending", async () => {
    const sent: unknown[] = [];
    server.use(http.get(`/api/v1/roots/${ROOT_ID}/entries`, ({request}) => {
      const url = new URL(request.url);
      sent.push(JSON.parse(url.searchParams.get("filters")!));
      return HttpResponse.json({...page(), directoryId: url.searchParams.get("parent") ?? ENTRY_ID, entries: []});
    }));
    const searched = filtersFromDraft({...emptyFilterDraft(), type: ["jpg"]}, "Trip 2026");
    if (!searched.ok) throw new Error(JSON.stringify(searched.errors));
    await fetchDirectory({...input, parentId: null, scope: "root", filters: searched.filters},
      new AbortController().signal);
    await fetchDirectory({...input, filters: {v: 1, contains: "😀".repeat(255)}}, new AbortController().signal);
    expect(sent).toEqual([{v: 1, contains: "Trip 2026", type: ["jpg"]}, {v: 1, contains: "😀".repeat(255)}]);

    for (const contains of ["", "   ", "x".repeat(256), 7]) {
      await expect(fetchDirectory(
        {...input, filters: {v: 1, contains} as unknown as BrowseInput["filters"]},
        new AbortController().signal,
      )).rejects.toBeDefined();
    }
    expect(sent).toHaveLength(2);
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
        downloadUrl: null,
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

  it("accepts the same-origin attachment URL of a present file in details", async () => {
    const downloadUrl = `/media/v1/originals/${ENTRY_ID}?d=attachment&n=${NAMESPACE}&e=7`;
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json({
      ...summary(), downloadUrl, parentId: DIRECTORY_ID, ancestors: [], ancestorsTruncated: false,
    })));

    await expect(fetchEntry(ENTRY_ID, new AbortController().signal)).resolves.toMatchObject({
      id: ENTRY_ID, downloadUrl,
    });
  });

  it.each([
    ["a missing field", {downloadUrl: undefined}],
    ["a non-string", {downloadUrl: 7}],
    ["an empty string", {downloadUrl: ""}],
    ["another entry's URL", {downloadUrl: `/media/v1/originals/${DIRECTORY_ID}?d=attachment&n=a&e=7`}],
    ["an inline URL", {downloadUrl: `/media/v1/originals/${ENTRY_ID}?d=inline&n=a&e=7`}],
    ["a derivative URL", {downloadUrl: `/media/v1/derivatives/${ENTRY_ID}/preview?f=${"a".repeat(32)}&n=a&e=7`}],
    ["a URL without the namespace", {downloadUrl: `/media/v1/originals/${ENTRY_ID}?d=attachment&e=7`}],
    ["a URL with an empty epoch", {downloadUrl: `/media/v1/originals/${ENTRY_ID}?d=attachment&n=a&e=`}],
    ["another origin", {downloadUrl: `https://files.invalid/media/v1/originals/${ENTRY_ID}?d=attachment&n=a&e=7`}],
    ["a protocol-relative URL", {downloadUrl: `//files.invalid/media/v1/originals/${ENTRY_ID}?d=attachment&n=a&e=7`}],
    ["a fragment", {downloadUrl: `/media/v1/originals/${ENTRY_ID}?d=attachment&n=a&e=7#x`}],
    ["a dot segment", {downloadUrl: `/media/v1/originals/../originals/${ENTRY_ID}?d=attachment&n=a&e=7`}],
    ["a directory's URL", {kind: "directory", typeHint: null, size: null}],
    ["a symbolic link's URL", {kind: "symlink"}],
    ["a missing file's URL", {sourceState: "missing"}],
    ["an inaccessible file's URL", {sourceState: "inaccessible"}],
  ])("rejects details whose download URL is %s", async (_name, overrides) => {
    const changes: Record<string, unknown> = overrides;
    const body: Record<string, unknown> = {
      ...summary(),
      downloadUrl: `/media/v1/originals/${ENTRY_ID}?d=attachment&n=a&e=7`,
      parentId: DIRECTORY_ID,
      ancestors: [],
      ancestorsTruncated: false,
      ...changes,
    };
    if ("downloadUrl" in changes && changes.downloadUrl === undefined) delete body.downloadUrl;
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(body)));

    await expect(fetchEntry(ENTRY_ID, new AbortController().signal)).rejects.toMatchObject({status: 502});
  });

  it("accepts details without a download URL for every kind and state", async () => {
    for (const [kind, sourceState] of [
      ["file", "present"], ["file", "missing"], ["directory", "present"], ["special", "unsupported"],
    ] as const) {
      server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json({
        ...summary(), kind, sourceState, downloadUrl: null, parentId: null, ancestors: [],
        ancestorsTruncated: false,
      })));
      await expect(fetchEntry(ENTRY_ID, new AbortController().signal)).resolves.toMatchObject({
        kind, sourceState, downloadUrl: null,
      });
    }
  });

  it("rejects more than 64 ancestors and malformed scan/status bodies", async () => {
    server.use(
      http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json({
        ...summary(),
        downloadUrl: null,
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
      // Even its own entry's download URL: the whole body names another entry.
      downloadUrl: `/media/v1/originals/${otherId}?d=attachment&n=${NAMESPACE}&e=7`,
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
