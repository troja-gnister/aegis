import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {MemoryRouter, Route, Routes, useLocation, type InitialEntry} from "react-router";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {server} from "../../test/server";
import {AppRoutes} from "../../app/router";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {AuthSessionContext} from "../auth/session";
import type {SessionResponse} from "../auth/types";
import {FileNavigationProvider} from "../files/FilesPage";
import {FIXTURE_ROOT_ID} from "../files/test-fixtures";
import {PhotosPage} from "./PhotosPage";
import {FIRST_TIMELINE_PAGE} from "./queries";
import {
  derivativeUrlFor,
  mediaDetailsBody,
  timelineItemBody,
  timelineItemId,
  timelinePageBody,
} from "./test-fixtures";
import {rememberViewerReturn} from "./viewer-return";
import {ViewerRoute} from "./viewer/ViewerRoute";

const SESSION: SessionResponse = {
  user: {id: "1797b1eb-c642-4f66-99a4-a878347b4e49", username: "alice"},
  cacheNamespace: "photos-page-namespace",
};
const TIMELINE_PATH = `/api/v1/roots/${FIXTURE_ROOT_ID}/timeline`;
const PHOTOS_PATH = `/files/${FIXTURE_ROOT_ID}/photos`;
const clients = new Set<QueryClient>();

function rootsHandler(permissions = ["browse", "preview", "export"]) {
  return http.get("/api/v1/roots", () => HttpResponse.json({roots: [{
    id: FIXTURE_ROOT_ID,
    displayName: "Synthetic archive",
    mode: "read_only",
    permissions,
    authorizationEpoch: 4,
  }]}));
}

function statusHandler(state: string) {
  return http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/index-status`, () => HttpResponse.json({
    state,
    generation: "1",
    observedEntries: "10",
    completedDirectories: "1",
    degradedDirectories: "0",
    updatedAt: "2026-09-21T12:00:00Z",
    lastCompletedAt: state === "not_indexed" ? null : "2026-09-21T12:00:00Z",
  }));
}

function mediaDetailsHandler() {
  return http.get("/api/v1/entries/:entryId/media", ({params}) => {
    const entryId = String(params.entryId);
    return HttpResponse.json(mediaDetailsBody(entryId, {derivatives: {
      thumbnail: {state: "ready", url: derivativeUrlFor(entryId, "thumbnail")},
      preview: {state: "ready", url: derivativeUrlFor(entryId, "preview")},
    }}));
  });
}

function items(start: number, length: number, kind: "photo" | "video" = "photo") {
  return Array.from({length}, (_, offset) => timelineItemBody(start + offset, {kind}));
}

/** Serves pages of six items of the requested kind; records every request's search parameters. */
function timelineHandler(requests: URLSearchParams[], pageCount = 1) {
  return http.get(TIMELINE_PATH, ({request}) => {
    const parameters = new URL(request.url).searchParams;
    requests.push(parameters);
    const cursor = parameters.get("cursor");
    const page = cursor === null ? 0 : Number(cursor.slice("page-".length));
    const kind = parameters.get("kind") === "video" ? "video" : "photo";
    return HttpResponse.json(
      timelinePageBody(items(page * 6, 6, kind), page + 1 < pageCount ? `page-${page + 1}` : null),
    );
  });
}

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <output aria-label="Current route">{`${location.pathname}${location.search}`}</output>
      <output aria-label="History state">{JSON.stringify(location.state)}</output>
    </>
  );
}

async function renderPhotos(path: InitialEntry = PHOTOS_PATH) {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
  render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <MemoryRouter initialEntries={[path]}>
          <FileNavigationProvider>
            <LocationProbe />
            <Routes>
              <Route path="/files/:rootId/photos" element={<PhotosPage />} />
              <Route path="/files/:rootId/view/:entryId" element={<ViewerRoute />} />
              <Route path="/files/:rootId" element={<h1>Files route</h1>} />
            </Routes>
          </FileNavigationProvider>
        </MemoryRouter>
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
  return queryClient;
}

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(640);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(320);
});

afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all([...clients].map((queryClient) => purgePrivateBrowserState(queryClient)));
  clients.clear();
});

describe("PhotosPage", () => {
  it("shows the root's timeline with Files and Photos navigation", async () => {
    const requests: URLSearchParams[] = [];
    server.use(rootsHandler(), timelineHandler(requests));
    await renderPhotos();

    expect(await screen.findByRole("heading", {level: 1, name: "Synthetic archive"})).toBeVisible();
    expect(await screen.findByRole("link", {name: "Open photo Synthetic photo 0000.jpg"})).toBeVisible();
    const views = screen.getByRole("navigation", {name: "Root views"});
    expect(views.querySelector("[aria-current='page']")).toHaveTextContent("Photos");
    expect(screen.getByRole("link", {name: "Files"})).toHaveAttribute("href", `/files/${FIXTURE_ROOT_ID}`);
    expect(requests[0]!.get("kind")).toBe("all");
    expect(requests[0]!.has("year")).toBe(false);
  });

  it("keeps the kind filter in the URL only", async () => {
    const requests: URLSearchParams[] = [];
    server.use(rootsHandler(), timelineHandler(requests));
    await renderPhotos();
    await screen.findByRole("link", {name: "Open photo Synthetic photo 0000.jpg"});

    fireEvent.click(screen.getByRole("button", {name: "Videos"}));

    await waitFor(() => expect(requests.at(-1)!.get("kind")).toBe("video"));
    expect(screen.getByLabelText("Current route")).toHaveTextContent(`${PHOTOS_PATH}?kind=video`);
    expect(screen.getByLabelText("History state")).toHaveTextContent("null");
    expect(screen.getByRole("button", {name: "Videos"})).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", {name: "All"})).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(screen.getByRole("button", {name: "All"}));

    await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent(new RegExp(`${PHOTOS_PATH}$`)));
    await waitFor(() => expect(requests.at(-1)!.get("kind")).toBe("all"));
  });

  it("reads the kind filter and year from the URL", async () => {
    const requests: URLSearchParams[] = [];
    server.use(rootsHandler(), timelineHandler(requests));
    await renderPhotos(`${PHOTOS_PATH}?kind=photo&year=2027`);

    await screen.findByRole("link", {name: "Open photo Synthetic photo 0000.jpg"});
    expect(requests[0]!.get("kind")).toBe("photo");
    expect(requests[0]!.get("year")).toBe("2027");
    expect(screen.getByRole("button", {name: "Photos"})).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", {name: "2027 · Show every year"}));
    await waitFor(() => expect(requests.at(-1)!.has("year")).toBe(false));
    expect(screen.getByLabelText("Current route")).toHaveTextContent(`${PHOTOS_PATH}?kind=photo`);
  });

  it.each([
    ["ready", "", "No photos or videos in this location yet."],
    ["ready", "?kind=video", "No videos in this location yet."],
    ["scanning", "", "Indexing this location… Photos and videos appear here as they are found."],
    ["queued", "?kind=photo", "Indexing this location… Photos and videos appear here as they are found."],
    ["not_indexed", "", "This location has not been indexed yet. Photos and videos appear after the first scan."],
  ])("explains an empty timeline while the index is %s (%s)", async (state, search, message) => {
    server.use(
      rootsHandler(),
      statusHandler(state),
      http.get(TIMELINE_PATH, () => HttpResponse.json(timelinePageBody([]))),
    );
    await renderPhotos(`${PHOTOS_PATH}${search}`);
    expect(await screen.findByText(message)).toBeVisible();
    expect(screen.queryByRole("link", {name: /^Open/})).not.toBeInTheDocument();
  });

  it("offers a retry when the timeline cannot be loaded", async () => {
    let fail = true;
    server.use(rootsHandler(), http.get(TIMELINE_PATH, () => (fail
      ? HttpResponse.json({type: "catalog_unavailable", title: "Catalog unavailable"}, {status: 503})
      : HttpResponse.json(timelinePageBody(items(0, 2))))));
    await renderPhotos();

    expect(await screen.findByText("Photos could not be loaded.")).toBeVisible();
    fail = false;
    fireEvent.click(screen.getByRole("button", {name: "Try again"}));
    expect(await screen.findByRole("link", {name: "Open photo Synthetic photo 0000.jpg"})).toBeVisible();
    expect(screen.queryByText("Photos could not be loaded.")).not.toBeInTheDocument();
  });

  it("restarts from the newest items when a cursor is refused", async () => {
    const cursors: (string | null)[] = [];
    let refuse = true;
    server.use(rootsHandler(), http.get(TIMELINE_PATH, ({request}) => {
      const cursor = new URL(request.url).searchParams.get("cursor");
      cursors.push(cursor);
      if (cursor !== null && refuse) {
        return HttpResponse.json(
          {type: "cursor_restart_required", title: "Start again"},
          {status: 409},
        );
      }
      return HttpResponse.json(timelinePageBody(items(0, 6), cursor === null ? "page-1" : null));
    }));
    await renderPhotos();
    await screen.findByRole("link", {name: "Open photo Synthetic photo 0000.jpg"});

    // Six items do not fill the viewport, so the grid asks for the next page by itself.
    expect(await screen.findByText("This timeline changed. Start from the newest items to keep browsing.")).toBeVisible();
    expect(cursors).toContain("page-1");

    refuse = false;
    const requestsBefore = cursors.length;
    fireEvent.click(screen.getByRole("button", {name: "Start from the newest items"}));

    await waitFor(() => expect(cursors.length).toBeGreaterThan(requestsBefore));
    expect(cursors[requestsBefore]).toBeNull();
    // The stale window's tiles would pass a link query; the alert going away proves the restart.
    await waitFor(() => expect(
      screen.queryByText("This timeline changed. Start from the newest items to keep browsing."),
    ).not.toBeInTheDocument());
    expect(await screen.findByRole("link", {name: "Open photo Synthetic photo 0000.jpg"})).toBeVisible();
  });

  it("restarts a returning window at its stored page when nothing was held", async () => {
    const requests: URLSearchParams[] = [];
    server.use(rootsHandler(), timelineHandler(requests, 2), mediaDetailsHandler());
    rememberViewerReturn(SESSION.cacheNamespace, "back1", {
      entryId: timelineItemId(7),
      openedEntryId: timelineItemId(2),
      anchorOffset: 0,
      timelinePageParam: {cursor: "page-1", index: 1, previous: FIRST_TIMELINE_PAGE},
    });
    await renderPhotos({pathname: PHOTOS_PATH, key: "back1"});

    await waitFor(() => expect(document.activeElement).toHaveAccessibleName("Open photo Synthetic photo 0007.jpg"));
    expect(requests[0]!.get("cursor")).toBe("page-1");
    expect(screen.getByRole("button", {name: "Load newer items"})).toBeVisible();
  });

  it("restarts quietly from the newest items when a stored place has expired", async () => {
    const cursors: (string | null)[] = [];
    server.use(rootsHandler(), http.get(TIMELINE_PATH, ({request}) => {
      const cursor = new URL(request.url).searchParams.get("cursor");
      cursors.push(cursor);
      if (cursor !== null) {
        return HttpResponse.json({type: "cursor_restart_required", title: "Start again"}, {status: 409});
      }
      return HttpResponse.json(timelinePageBody(items(0, 6)));
    }));
    rememberViewerReturn(SESSION.cacheNamespace, "back2", {
      entryId: timelineItemId(70),
      openedEntryId: timelineItemId(70),
      anchorOffset: 0,
      timelinePageParam: {cursor: "page-11", index: 11, previous: null},
    });
    await renderPhotos({pathname: PHOTOS_PATH, key: "back2"});

    expect(await screen.findByText("Your place expired; showing the newest items.", {selector: "p.notice"}))
      .toBeVisible();
    expect(await screen.findByRole("link", {name: "Open photo Synthetic photo 0000.jpg"})).toBeVisible();
    expect(cursors).toEqual(["page-11", null]);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows tiles as plain pictures with a notice when the root lacks PREVIEW", async () => {
    const requests: URLSearchParams[] = [];
    server.use(rootsHandler(["browse"]), timelineHandler(requests));
    await renderPhotos();

    expect(await screen.findByText("Your access to this location does not include viewing photos and videos."))
      .toBeVisible();
    await waitFor(() => expect(screen.getByTitle("Synthetic photo 0000.jpg")).toBeInTheDocument());
    expect(screen.queryByRole("link", {name: /^Open/})).not.toBeInTheDocument();
  });

  it("takes back the held window after Back through the session check, without refetching", async () => {
    const requests: URLSearchParams[] = [];
    let sessionCalls = 0;
    server.use(
      http.get("/api/v1/auth/session", () => {
        sessionCalls += 1;
        return HttpResponse.json(SESSION);
      }),
      rootsHandler(),
      timelineHandler(requests),
      mediaDetailsHandler(),
    );
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[PHOTOS_PATH]}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("link", {name: "Open photo Synthetic photo 0002.jpg"}));
    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo 0002.jpg"});
    fireEvent.keyDown(dialog, {key: "ArrowRight"});
    await screen.findByRole("dialog", {name: "Synthetic photo 0003.jpg"});
    const timelineRequests = requests.length;
    const checksBeforeBack = sessionCalls;

    fireEvent.keyDown(screen.getByRole("dialog"), {key: "Escape"});

    await waitFor(() => expect(sessionCalls).toBe(checksBeforeBack + 1));
    await waitFor(() => expect(document.activeElement).toHaveAccessibleName("Open photo Synthetic photo 0003.jpg"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(requests).toHaveLength(timelineRequests);
  });

  it("opens the viewer with the timeline context in history state, IDs only", async () => {
    const requests: URLSearchParams[] = [];
    server.use(rootsHandler(), timelineHandler(requests), mediaDetailsHandler());
    await renderPhotos(`${PHOTOS_PATH}?kind=photo`);

    fireEvent.click(await screen.findByRole("link", {name: "Open photo Synthetic photo 0002.jpg"}));

    expect(screen.getByLabelText("Current route")).toHaveTextContent(`/files/${FIXTURE_ROOT_ID}/view/${timelineItemId(2)}`);
    const stateText = screen.getByLabelText("History state").textContent ?? "";
    expect(JSON.parse(stateText)).toEqual({
      aegisMediaViewer: {
        v: 1,
        entryId: timelineItemId(2),
        context: {source: "timeline", rootId: FIXTURE_ROOT_ID, returnKey: "default", kind: "photo", year: null},
      },
    });
    expect(stateText).not.toContain("Synthetic");
    expect(await screen.findByRole("dialog", {name: "Synthetic photo 0002.jpg"})).toBeVisible();
  });

  it("returns focus and scroll to the tile the viewer showed last", async () => {
    const requests: URLSearchParams[] = [];
    server.use(rootsHandler(), timelineHandler(requests), mediaDetailsHandler());
    await renderPhotos();

    fireEvent.click(await screen.findByRole("link", {name: "Open photo Synthetic photo 0002.jpg"}));
    const dialog = await screen.findByRole("dialog", {name: "Synthetic photo 0002.jpg"});
    fireEvent.keyDown(dialog, {key: "ArrowRight"});
    await screen.findByRole("dialog", {name: "Synthetic photo 0003.jpg"});

    fireEvent.keyDown(screen.getByRole("dialog"), {key: "Escape"});

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Current route")).toHaveTextContent(new RegExp(`${PHOTOS_PATH}$`));
    await waitFor(() => expect(document.activeElement).toHaveAccessibleName("Open photo Synthetic photo 0003.jpg"));
  });

  it("explains an unassigned root", async () => {
    server.use(http.get("/api/v1/roots", () => HttpResponse.json({roots: []})));
    await renderPhotos();
    expect(await screen.findByRole("heading", {name: "Root unavailable"})).toBeVisible();
  });
});
