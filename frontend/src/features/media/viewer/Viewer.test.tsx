import {focusManager, QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {act, fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {MemoryRouter, Route, Routes, useLocation, type InitialEntry} from "react-router";
import {afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi} from "vitest";
import {installPointerEvents} from "../../../test/pointer-events";
import {server} from "../../../test/server";
import {AppRoutes} from "../../../app/router";
import {activateCacheNamespace, purgePrivateBrowserState} from "../../auth/cache";
import {AuthSessionContext, isSessionAccessOpen} from "../../auth/session";
import type {SessionResponse} from "../../auth/types";
import {FileNavigationProvider, FilesPage} from "../../files/FilesPage";
import {FIXTURE_ROOT_ID, fixtureEntry} from "../../files/test-fixtures";
import {
  derivativeUrlFor,
  entryMediaFixture,
  mediaDetailsBody,
  originalUrlFor,
  timelineItemBody,
  timelineItemId,
  timelinePageBody,
} from "../test-fixtures";
import type {MediaViewerContext} from "../types";
import {viewerLocationState, viewerPath} from "../viewer-route";
import {ViewerRoute} from "./ViewerRoute";

const PARENT_ID = "22222222-2222-4222-8222-222222222222";
const PHOTOS_PATH = `/files/${FIXTURE_ROOT_ID}/photos`;
const SESSION: SessionResponse = {
  user: {id: "1797b1eb-c642-4f66-99a4-a878347b4e49", username: "alice"},
  cacheNamespace: "viewer-namespace",
};
const clients = new Set<QueryClient>();

let restorePointerEvents = () => {};

function rootsHandler(permissions = ["browse", "preview", "export"]) {
  return http.get("/api/v1/roots", () => HttpResponse.json({roots: [{
    id: FIXTURE_ROOT_ID,
    displayName: "Synthetic archive",
    mode: "read_only",
    permissions,
    authorizationEpoch: 4,
  }]}));
}

/** Media details with every viewing derivative ready, so no request is made. */
function readyDetails(entryId: string, kind: "photo" | "video", exportAllowed: boolean) {
  return mediaDetailsBody(entryId, {
    kind,
    permissions: {preview: true, export: exportAllowed},
    derivatives: kind === "video"
      ? {
        thumbnail: {state: "ready", url: derivativeUrlFor(entryId, "thumbnail")},
        poster: {state: "ready", url: derivativeUrlFor(entryId, "poster")},
        transcode: {state: "ready", url: derivativeUrlFor(entryId, "transcode")},
      }
      : {
        thumbnail: {state: "ready", url: derivativeUrlFor(entryId, "thumbnail")},
        preview: {state: "ready", url: derivativeUrlFor(entryId, "preview")},
      },
  });
}

function mediaHandler({exportAllowed = true, videos = new Set<string>()} = {}) {
  return http.get("/api/v1/entries/:entryId/media", ({params}) => {
    const entryId = String(params.entryId);
    return HttpResponse.json(readyDetails(entryId, videos.has(entryId) ? "video" : "photo", exportAllowed));
  });
}

/** Timeline pages of three photos; `pageCount` pages linked by `page-N` cursors. */
function timelineHandler(cursors: (string | null)[] = [], pageCount = 1) {
  return http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/timeline`, ({request}) => {
    const cursor = new URL(request.url).searchParams.get("cursor");
    cursors.push(cursor);
    const page = cursor === null ? 0 : Number(cursor.slice("page-".length));
    const items = [0, 1, 2].map((offset) => timelineItemBody(page * 3 + offset));
    return HttpResponse.json(timelinePageBody(items, page + 1 < pageCount ? `page-${page + 1}` : null));
  });
}

function timelineEntries(entryId: string): InitialEntry[] {
  const context: MediaViewerContext = {
    source: "timeline", rootId: FIXTURE_ROOT_ID, returnKey: "opener1", kind: "all", year: null,
  };
  return [
    {pathname: PHOTOS_PATH, key: "opener1"},
    {pathname: viewerPath(FIXTURE_ROOT_ID, entryId), state: viewerLocationState(entryId, context)},
  ];
}

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <output aria-label="Current route">{location.pathname}</output>
      <output aria-label="History state">{JSON.stringify(location.state)}</output>
    </>
  );
}

async function renderViewer(entries: InitialEntry[]) {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
  render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
          <FileNavigationProvider>
            <LocationProbe />
            <button type="button">Outside the viewer</button>
            <Routes>
              <Route path="/files/:rootId/photos" element={<h1>Photos opener</h1>} />
              <Route path="/files/:rootId" element={<h1>Files opener</h1>} />
              <Route path="/files/:rootId/directories/:parentId" element={<h1>Directory opener</h1>} />
              <Route path="/files/:rootId/view/:entryId" element={<ViewerRoute />} />
            </Routes>
          </FileNavigationProvider>
        </MemoryRouter>
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
  return queryClient;
}

function route(): string {
  return screen.getByLabelText("Current route").textContent ?? "";
}

function historyState(): {aegisMediaViewer: {entryId: string; context: unknown}} {
  return JSON.parse(screen.getByLabelText("History state").textContent ?? "null");
}

beforeAll(() => {
  restorePointerEvents = installPointerEvents();
});

afterAll(() => restorePointerEvents());

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(320);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(640);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
});

afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all([...clients].map((queryClient) => purgePrivateBrowserState(queryClient)));
  clients.clear();
});

describe("Viewer in the timeline context", () => {
  it("moves through the timeline with the keyboard and shows the position", async () => {
    server.use(rootsHandler(), timelineHandler(), mediaHandler());
    await renderViewer(timelineEntries(timelineItemId(1)));

    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(await within(dialog).findByText("2 of 3")).toBeVisible();

    fireEvent.keyDown(dialog, {key: "ArrowRight"});

    await screen.findByRole("dialog", {name: "Synthetic photo 0002.jpg"});
    expect(route()).toBe(viewerPath(FIXTURE_ROOT_ID, timelineItemId(2)));
    expect(historyState().aegisMediaViewer).toEqual({
      v: 1,
      entryId: timelineItemId(2),
      context: {source: "timeline", rootId: FIXTURE_ROOT_ID, returnKey: "opener1", kind: "all", year: null},
    });
    expect(screen.getByText("3 of 3")).toBeVisible();
    expect(screen.queryByRole("button", {name: /^Next/})).not.toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole("dialog"), {key: "ArrowRight"});
    expect(route()).toBe(viewerPath(FIXTURE_ROOT_ID, timelineItemId(2)));

    fireEvent.keyDown(screen.getByRole("dialog"), {key: "ArrowLeft"});
    await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});
    fireEvent.keyDown(screen.getByRole("dialog"), {key: "ArrowLeft"});
    await screen.findByRole("dialog", {name: "Synthetic photo 0000.jpg"});
    expect(screen.getByText("1 of 3")).toBeVisible();
    expect(JSON.stringify(historyState())).not.toContain("Synthetic");
  });

  it("closes with Escape back to the opener", async () => {
    server.use(rootsHandler(), timelineHandler(), mediaHandler());
    await renderViewer(timelineEntries(timelineItemId(1)));
    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});

    fireEvent.keyDown(dialog, {key: "Escape"});

    expect(await screen.findByRole("heading", {name: "Photos opener"})).toBeVisible();
    expect(route()).toBe(PHOTOS_PATH);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes with the close button", async () => {
    server.use(rootsHandler(), timelineHandler(), mediaHandler());
    await renderViewer(timelineEntries(timelineItemId(1)));
    fireEvent.click(await screen.findByRole("button", {name: "Close viewer"}));
    expect(await screen.findByRole("heading", {name: "Photos opener"})).toBeVisible();
  });

  it("fetches the next page when the next item is not loaded yet", async () => {
    const cursors: (string | null)[] = [];
    server.use(rootsHandler(), timelineHandler(cursors, 3), mediaHandler());
    await renderViewer(timelineEntries(timelineItemId(2)));
    await screen.findByRole("dialog", {name: "Synthetic photo 0002.jpg"});

    const next = await screen.findByRole("button", {name: "Next photo"});
    expect(cursors).toEqual([null, "page-1"]);
    // Not every item is known, so no position is claimed.
    expect(screen.queryByText(/ of /)).not.toBeInTheDocument();

    fireEvent.click(next);
    await screen.findByRole("dialog", {name: "Synthetic photo 0003.jpg"});
    expect(route()).toBe(viewerPath(FIXTURE_ROOT_ID, timelineItemId(3)));
  });

  it("fetches an unloaded neighbor's page once, and offers a retry when it fails", async () => {
    let allow = false;
    let secondPageRequests = 0;
    server.use(rootsHandler(), mediaHandler(), http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/timeline`, ({request}) => {
      const cursor = new URL(request.url).searchParams.get("cursor");
      if (cursor === null) {
        return HttpResponse.json(timelinePageBody([0, 1, 2].map((index) => timelineItemBody(index)), "page-1"));
      }
      secondPageRequests += 1;
      if (!allow) {
        return HttpResponse.json({type: "catalog_unavailable", title: "Catalog unavailable"}, {status: 503});
      }
      return HttpResponse.json(timelinePageBody([3, 4, 5].map((index) => timelineItemBody(index))));
    }));
    await renderViewer(timelineEntries(timelineItemId(2)));

    expect(await screen.findByText("More items could not be loaded.")).toBeVisible();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(secondPageRequests).toBe(1);
    expect(screen.queryByRole("button", {name: /^Next/})).not.toBeInTheDocument();

    allow = true;
    fireEvent.click(screen.getByRole("button", {name: "Retry loading items"}));

    expect(await screen.findByRole("button", {name: "Next photo"})).toBeVisible();
    expect(secondPageRequests).toBe(2);
    expect(screen.queryByText("More items could not be loaded.")).not.toBeInTheDocument();
  });

  it("treats a refused cursor as the end of the context, without retrying", async () => {
    let secondPageRequests = 0;
    server.use(rootsHandler(), mediaHandler(), http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/timeline`, ({request}) => {
      if (new URL(request.url).searchParams.get("cursor") === null) {
        return HttpResponse.json(timelinePageBody([0, 1, 2].map((index) => timelineItemBody(index)), "page-1"));
      }
      secondPageRequests += 1;
      return HttpResponse.json({type: "cursor_restart_required", title: "Start again"}, {status: 409});
    }));
    await renderViewer(timelineEntries(timelineItemId(2)));
    await screen.findByRole("dialog", {name: "Synthetic photo 0002.jpg"});

    await waitFor(() => expect(secondPageRequests).toBe(1));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(secondPageRequests).toBe(1);
    expect(screen.queryByText("More items could not be loaded.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /^Next/})).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Previous photo"})).toBeVisible();
  });

  it("swipes left and right between photos", async () => {
    server.use(rootsHandler(), timelineHandler(), mediaHandler());
    await renderViewer(timelineEntries(timelineItemId(1)));
    await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});

    const swipe = (from: number, to: number) => {
      const stage = screen.getByTestId("photo-stage");
      fireEvent.pointerDown(stage, {pointerId: 1, pointerType: "touch", clientX: from, clientY: 300});
      fireEvent.pointerMove(stage, {pointerId: 1, pointerType: "touch", clientX: (from + to) / 2, clientY: 300});
      fireEvent.pointerUp(stage, {pointerId: 1, pointerType: "touch", clientX: to, clientY: 302});
    };

    swipe(280, 60);
    await screen.findByRole("dialog", {name: "Synthetic photo 0002.jpg"});
    swipe(60, 280);
    await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});
    expect(route()).toBe(viewerPath(FIXTURE_ROOT_ID, timelineItemId(1)));
  });

  it("prefetches the neighbors' previews on same-origin URLs", async () => {
    const sources: string[] = [];
    class RecordingImage {
      decoding = "auto";
      set src(value: string) {
        sources.push(value);
      }
    }
    vi.stubGlobal("Image", RecordingImage);
    try {
      server.use(rootsHandler(), timelineHandler(), mediaHandler());
      await renderViewer(timelineEntries(timelineItemId(1)));
      await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});

      await waitFor(() => expect(sources).toEqual(expect.arrayContaining([
        derivativeUrlFor(timelineItemId(0), "preview"),
        derivativeUrlFor(timelineItemId(2), "preview"),
      ])));
      // Only the neighbors are warmed; the shown item loads through its own image.
      expect(sources).not.toContain(derivativeUrlFor(timelineItemId(1), "preview"));
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("Viewer in the directory context", () => {
  const directory = {...fixtureEntry(0), displayName: "Nested", kind: "directory" as const, typeHint: null, size: null};
  const photo = {
    ...fixtureEntry(1),
    displayName: "Synthetic photo A.jpg",
    media: entryMediaFixture(fixtureEntry(1).id),
  };
  const notes = {...fixtureEntry(2), displayName: "Notes.txt", typeHint: "txt"};
  const clip = {
    ...fixtureEntry(3),
    displayName: "Synthetic clip B.mp4",
    typeHint: "mp4",
    media: entryMediaFixture(fixtureEntry(3).id, {kind: "video", durationMs: 5000}),
  };

  function directoryHandler(parents: (string | null)[] = []) {
    return http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
      parents.push(new URL(request.url).searchParams.get("parent"));
      return HttpResponse.json({
        entries: [directory, photo, notes, clip],
        nextCursor: null,
        previousCursor: null,
        directoryId: PARENT_ID,
        directoryVersion: "1",
        contractVersion: 1,
        indexStatus: {
          state: "ready", generation: "1", observedEntries: "4", completedDirectories: "1",
          degradedDirectories: "0", updatedAt: "2026-09-21T12:00:00Z", lastCompletedAt: "2026-09-21T12:00:00Z",
        },
      });
    });
  }

  it("moves through the directory's media entries only", async () => {
    const parents: (string | null)[] = [];
    server.use(rootsHandler(), directoryHandler(parents), mediaHandler({videos: new Set([clip.id])}));
    const context: MediaViewerContext = {
      source: "directory", rootId: FIXTURE_ROOT_ID, parentId: PARENT_ID, returnKey: "opener2",
    };
    await renderViewer([
      {pathname: `/files/${FIXTURE_ROOT_ID}/directories/${PARENT_ID}`, key: "opener2"},
      {pathname: viewerPath(FIXTURE_ROOT_ID, photo.id), state: viewerLocationState(photo.id, context)},
    ]);

    await screen.findByRole("dialog", {name: "Synthetic photo A.jpg"});
    expect(parents).toEqual([PARENT_ID]);
    expect(await screen.findByText("1 of 2")).toBeVisible();
    expect(screen.queryByRole("button", {name: /^Previous/})).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", {name: "Next video"}));

    await screen.findByRole("dialog", {name: "Synthetic clip B.mp4"});
    expect(route()).toBe(viewerPath(FIXTURE_ROOT_ID, clip.id));
    expect(historyState().aegisMediaViewer.context).toEqual(context);
    expect(screen.getByText("2 of 2")).toBeVisible();
    expect(screen.getByRole("button", {name: "Previous photo"})).toBeVisible();
    expect(await screen.findByTestId("video-stage")).toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole("dialog"), {key: "Escape"});
    expect(await screen.findByRole("heading", {name: "Directory opener"})).toBeVisible();
  });

  it("stops paging a sparse directory after one page without media, keeping the photo in place", async () => {
    const pageRequests: (string | null)[] = [];
    server.use(rootsHandler(), mediaHandler(), http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
      const cursor = new URL(request.url).searchParams.get("cursor");
      pageRequests.push(cursor);
      const page = cursor === null ? 0 : Number(cursor.slice("p".length));
      const entries = page === 0
        ? [photo]
        : Array.from({length: 3}, (_, offset) => ({
          ...fixtureEntry(100 + page * 10 + offset),
          displayName: `Notes ${page}-${offset}.txt`,
          typeHint: "txt",
        }));
      return HttpResponse.json({
        entries,
        nextCursor: page < 6 ? `p${page + 1}` : null,
        previousCursor: page > 0 ? `p${page - 1}` : null,
        directoryId: PARENT_ID,
        directoryVersion: "1",
        contractVersion: 1,
        indexStatus: {
          state: "ready", generation: "1", observedEntries: "19", completedDirectories: "1",
          degradedDirectories: "0", updatedAt: "2026-09-21T12:00:00Z", lastCompletedAt: "2026-09-21T12:00:00Z",
        },
      });
    }));
    const context: MediaViewerContext = {
      source: "directory", rootId: FIXTURE_ROOT_ID, parentId: PARENT_ID, returnKey: "opener3",
    };
    await renderViewer([
      {pathname: `/files/${FIXTURE_ROOT_ID}/directories/${PARENT_ID}`, key: "opener3"},
      {pathname: viewerPath(FIXTURE_ROOT_ID, photo.id), state: viewerLocationState(photo.id, context)},
    ]);
    await screen.findByRole("dialog", {name: "Synthetic photo A.jpg"});

    await waitFor(() => expect(pageRequests).toEqual([null, "p1"]));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });

    expect(pageRequests).toEqual([null, "p1"]);
    expect(screen.getByRole("dialog", {name: "Synthetic photo A.jpg"})).toBeInTheDocument();
    expect(route()).toBe(viewerPath(FIXTURE_ROOT_ID, photo.id));
    expect(screen.queryByRole("button", {name: /^(Next|Previous)/})).not.toBeInTheDocument();
  });

  it("returns through the session check to the held file list, focused on the row shown last", async () => {
    let sessionCalls = 0;
    let directoryRequests = 0;
    server.use(
      http.get("/api/v1/auth/session", () => {
        sessionCalls += 1;
        return HttpResponse.json(SESSION);
      }),
      rootsHandler(),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, () => {
        directoryRequests += 1;
        return HttpResponse.json({
          entries: [photo, clip],
          nextCursor: null,
          previousCursor: null,
          directoryId: FIXTURE_ROOT_ID,
          directoryVersion: "1",
          contractVersion: 1,
          indexStatus: {
            state: "ready", generation: "1", observedEntries: "2", completedDirectories: "1",
            degradedDirectories: "0", updatedAt: "2026-09-21T12:00:00Z", lastCompletedAt: "2026-09-21T12:00:00Z",
          },
        });
      }),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/index-status`, () => HttpResponse.json({
        state: "ready", generation: "1", observedEntries: "2", completedDirectories: "1",
        degradedDirectories: "0", updatedAt: "2026-09-21T12:00:00Z", lastCompletedAt: "2026-09-21T12:00:00Z",
      })),
      http.get(`/api/v1/entries/${photo.id}`, () => HttpResponse.json({
        ...photo, parentId: null, ancestors: [{id: FIXTURE_ROOT_ID, displayName: ""}], ancestorsTruncated: false,
      })),
      mediaHandler({videos: new Set([clip.id])}),
    );
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/files/${FIXTURE_ROOT_ID}`]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", {name: "Select file Synthetic photo A.jpg"}));
    fireEvent.click(await screen.findByRole("link", {name: "Open photo"}));
    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo A.jpg"});
    fireEvent.keyDown(dialog, {key: "ArrowRight"});
    await screen.findByRole("dialog", {name: "Synthetic clip B.mp4"});
    const checksBeforeBack = sessionCalls;

    fireEvent.keyDown(screen.getByRole("dialog"), {key: "Escape"});

    // Back is a POP: private content waits for the session check, then the list comes back held.
    await waitFor(() => expect(sessionCalls).toBe(checksBeforeBack + 1));
    await waitFor(() => expect(document.activeElement).toHaveAccessibleName("Select file Synthetic clip B.mp4"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(directoryRequests).toBe(1);
  });

  it("returns focus to the file row the viewer showed last", async () => {
    server.use(
      rootsHandler(),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, () => HttpResponse.json({
        entries: [photo, clip],
        nextCursor: null,
        previousCursor: null,
        directoryId: FIXTURE_ROOT_ID,
        directoryVersion: "1",
        contractVersion: 1,
        indexStatus: {
          state: "ready", generation: "1", observedEntries: "2", completedDirectories: "1",
          degradedDirectories: "0", updatedAt: "2026-09-21T12:00:00Z", lastCompletedAt: "2026-09-21T12:00:00Z",
        },
      })),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/index-status`, () => HttpResponse.json({
        state: "ready", generation: "1", observedEntries: "2", completedDirectories: "1",
        degradedDirectories: "0", updatedAt: "2026-09-21T12:00:00Z", lastCompletedAt: "2026-09-21T12:00:00Z",
      })),
      http.get(`/api/v1/entries/${photo.id}`, () => HttpResponse.json({
        ...photo, parentId: null, ancestors: [{id: FIXTURE_ROOT_ID, displayName: ""}], ancestorsTruncated: false,
      })),
      mediaHandler({videos: new Set([clip.id])}),
    );
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
    render(
      <QueryClientProvider client={queryClient}>
        <AuthSessionContext.Provider value={{session: SESSION}}>
          <MemoryRouter initialEntries={[`/files/${FIXTURE_ROOT_ID}`]}>
            <FileNavigationProvider>
              <Routes>
                <Route path="/files/:rootId" element={<FilesPage />} />
                <Route path="/files/:rootId/view/:entryId" element={<ViewerRoute />} />
              </Routes>
            </FileNavigationProvider>
          </MemoryRouter>
        </AuthSessionContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", {name: "Select file Synthetic photo A.jpg"}));
    fireEvent.click(await screen.findByRole("link", {name: "Open photo"}));
    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo A.jpg"});
    fireEvent.keyDown(dialog, {key: "ArrowRight"});
    await screen.findByRole("dialog", {name: "Synthetic clip B.mp4"});

    fireEvent.keyDown(screen.getByRole("dialog"), {key: "Escape"});

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(document.activeElement).toHaveAccessibleName("Select file Synthetic clip B.mp4"));
  });
});

describe("Viewer toolbar and accessibility", () => {
  it("offers Download only with EXPORT", async () => {
    server.use(rootsHandler(), timelineHandler(), mediaHandler({exportAllowed: true}));
    await renderViewer(timelineEntries(timelineItemId(1)));

    const download = await screen.findByRole("link", {name: "Download original"});
    expect(download).toHaveAttribute("href", originalUrlFor(timelineItemId(1), "attachment"));
    expect(download).toHaveAttribute("download");
  });

  it("hides Download without EXPORT", async () => {
    server.use(rootsHandler(["browse", "preview"]), timelineHandler(), mediaHandler({exportAllowed: false}));
    await renderViewer(timelineEntries(timelineItemId(1)));
    await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});

    await waitFor(() => expect(
      document.querySelector(`img[src="${derivativeUrlFor(timelineItemId(1), "preview")}"]`),
    ).not.toBeNull());
    expect(screen.queryByRole("link", {name: "Download original"})).not.toBeInTheDocument();
  });

  it("names every control and keeps focus inside the dialog", async () => {
    server.use(rootsHandler(), timelineHandler(), mediaHandler());
    await renderViewer(timelineEntries(timelineItemId(1)));
    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});
    await waitFor(() => expect(document.activeElement).toBe(dialog));

    const close = screen.getByRole("button", {name: "Close viewer"});
    await screen.findByRole("link", {name: "Download original"});
    expect(screen.getByRole("button", {name: "Info"})).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("button", {name: "Previous photo"})).toBeVisible();
    const next = screen.getByRole("button", {name: "Next photo"});

    next.focus();
    fireEvent.keyDown(next, {key: "Tab"});
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(close, {key: "Tab", shiftKey: true});
    expect(document.activeElement).toBe(next);

    screen.getByRole("button", {name: "Outside the viewer"}).focus();
    expect(document.activeElement).toBe(dialog);
  });

  it("shows media details in the info panel and closes it before the viewer", async () => {
    server.use(rootsHandler(), timelineHandler(), mediaHandler());
    await renderViewer(timelineEntries(timelineItemId(1)));
    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});
    const info = screen.getByRole("button", {name: "Info"});

    fireEvent.click(info);

    expect(info).toHaveAttribute("aria-expanded", "true");
    const panel = screen.getByRole("complementary", {name: "Photo information"});
    expect(await within(panel).findByRole("heading", {name: "Photo details"})).toBeVisible();
    expect(within(panel).queryByRole("link", {name: "Download original"})).not.toBeInTheDocument();

    fireEvent.keyDown(dialog, {key: "Escape"});

    expect(screen.queryByRole("complementary", {name: "Photo information"})).not.toBeInTheDocument();
    expect(document.activeElement).toBe(info);
    expect(route()).toBe(viewerPath(FIXTURE_ROOT_ID, timelineItemId(1)));

    fireEvent.keyDown(dialog, {key: "Escape"});
    expect(await screen.findByRole("heading", {name: "Photos opener"})).toBeVisible();
  });

  it("shows a pasted link's entry alone and closes to the root's files", async () => {
    const entryId = timelineItemId(7);
    server.use(
      rootsHandler(),
      mediaHandler(),
      http.get(`/api/v1/entries/${entryId}`, () =>
        HttpResponse.json({...timelineItemBody(7), ancestors: [], ancestorsTruncated: false})),
    );
    await renderViewer([viewerPath(FIXTURE_ROOT_ID, entryId)]);

    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo 0007.jpg"});
    expect(screen.queryByRole("button", {name: /^(Next|Previous)/})).not.toBeInTheDocument();
    expect(screen.queryByText(/ of /)).not.toBeInTheDocument();

    fireEvent.keyDown(dialog, {key: "Escape"});
    expect(await screen.findByRole("heading", {name: "Files opener"})).toBeVisible();
    expect(route()).toBe(`/files/${FIXTURE_ROOT_ID}`);
  });

  it("ignores history state that belongs to another entry", async () => {
    const entryId = timelineItemId(1);
    server.use(
      rootsHandler(),
      mediaHandler(),
      http.get(`/api/v1/entries/${entryId}`, () => {
        const body = timelineItemBody(1);
        return HttpResponse.json({...body, ancestors: [], ancestorsTruncated: false});
      }),
    );
    const foreignState = viewerLocationState(timelineItemId(2), {
      source: "timeline", rootId: FIXTURE_ROOT_ID, returnKey: "opener1", kind: "all", year: null,
    });
    await renderViewer([{pathname: viewerPath(FIXTURE_ROOT_ID, entryId), state: foreignState}]);

    await screen.findByRole("dialog", {name: "Synthetic photo 0001.jpg"});
    expect(screen.queryByRole("button", {name: /^(Next|Previous)/})).not.toBeInTheDocument();
  });
});

describe("Viewer and logout", () => {
  it("closes when a background session check is refused, and the purge clears media state", async () => {
    const entryId = timelineItemId(4);
    let revoked = false;
    server.use(
      http.get("/api/v1/auth/session", () => revoked
        ? HttpResponse.json({type: "authentication_required", title: "Authentication required"}, {status: 401})
        : HttpResponse.json(SESSION)),
      rootsHandler(),
      mediaHandler(),
      http.get(`/api/v1/entries/${entryId}`, () => {
        const body = timelineItemBody(4);
        return HttpResponse.json({...body, ancestors: [], ancestorsTruncated: false});
      }),
    );
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[viewerPath(FIXTURE_ROOT_ID, entryId)]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByRole("dialog", {name: "Synthetic photo 0004.jpg"});
    expect(document.documentElement).toHaveClass("viewer-open");
    revoked = true;

    try {
      act(() => {
        focusManager.setFocused(false);
        focusManager.setFocused(true);
      });
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    } finally {
      focusManager.setFocused(undefined);
    }
    expect(await screen.findByRole("button", {name: "Sign in"})).toBeInTheDocument();
    expect(document.documentElement).not.toHaveClass("viewer-open");
    expect(isSessionAccessOpen()).toBe(false);
    expect(queryClient.getQueryCache().findAll({queryKey: ["media"]})).toHaveLength(0);
  });
});
