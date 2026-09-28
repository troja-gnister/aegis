import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {StrictMode, useEffect, useRef} from "react";
import {MemoryRouter, Route, Routes, useLocation, useNavigate} from "react-router";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {server} from "../../test/server";
import {AppRoutes} from "../../app/router";
import {activateCacheNamespace} from "../auth/cache";
import {AuthSessionContext} from "../auth/session";
import type {SessionResponse} from "../auth/types";
import {FileNavigationProvider, FileNavigationRouteConsumer, FilesPage} from "./FilesPage";
import type {FileNavigation, FileNavigationRecord} from "./navigation";
import {directoryQueryOptions} from "./queries";
import {FIXTURE_ROOT_ID, fixtureEntry} from "./test-fixtures";

const PARENT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION: SessionResponse = {
  user: {id: "1797b1eb-c642-4f66-99a4-a878347b4e49", username: "alice"},
  cacheNamespace: "files-page-namespace".repeat(3),
};

function rootResponse(roots = [{
  id: FIXTURE_ROOT_ID,
  displayName: "Synthetic archive",
  mode: "read_only",
  permissions: ["browse"],
  authorizationEpoch: 4,
}]) {
  return {roots};
}

function pageResponse(parentId: string | null, entries = [fixtureEntry(0)], state = "ready") {
  return {
    entries,
    nextCursor: null,
    previousCursor: null,
    directoryId: parentId ?? FIXTURE_ROOT_ID,
    directoryVersion: "1",
    contractVersion: 1,
    indexStatus: {
      state,
      generation: "1",
      observedEntries: String(entries.length),
      completedDirectories: "1",
      degradedDirectories: state === "degraded" ? "1" : "0",
      updatedAt: "2026-09-21T12:00:00Z",
      lastCompletedAt: "2026-09-21T12:00:00Z",
    },
  };
}

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function HistoryStateProbe() {
  return <output aria-label="History state">{JSON.stringify(useLocation().state)}</output>;
}

function BackButton() {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(-1)}>Back in history</button>;
}

function FilterSortRouteControl({navigation, rememberRoute}: {
  navigation: FileNavigation | null;
  rememberRoute(key: string, record: FileNavigationRecord): void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const pendingKey = useRef<string | null>(null);

  useEffect(() => {
    if (pendingKey.current === null || pendingKey.current === location.key) return;
    rememberRoute(location.key, {
      rootId: FIXTURE_ROOT_ID,
      parentId: null,
      filters: {v: 1, kind: ["file"]},
      sort: "size",
      order: "desc",
      cursor: null,
      visibleAnchorId: null,
      visibleAnchorOffset: 0,
    });
    pendingKey.current = null;
  }, [location.key, rememberRoute]);

  return (
    <button
      type="button"
      disabled={!navigation}
      onClick={() => {
        pendingKey.current = location.key;
        navigate(location.pathname);
      }}
    >
      Change filter and sort route
    </button>
  );
}

function NavigationRecordProbe() {
  const location = useLocation();
  return (
    <FileNavigationRouteConsumer>
      {({navigation}) => (
        <button
          type="button"
          onClick={(event) => {
            const record = navigation?.recall(location.key);
            event.currentTarget.dataset.record = JSON.stringify(record ? {
              filters: record.filters, cursor: record.cursor, anchor: record.visibleAnchorId,
            } : null);
          }}
        >
          Inspect navigation record
        </button>
      )}
    </FileNavigationRouteConsumer>
  );
}

function FilterSortRouteButton() {
  return (
    <FileNavigationRouteConsumer>
      {({navigation, rememberRoute}) => (
        <FilterSortRouteControl navigation={navigation} rememberRoute={rememberRoute} />
      )}
    </FileNavigationRouteConsumer>
  );
}

function liveStatusResponse(overrides: Record<string, unknown> = {}) {
  return {
    state: "ready",
    generation: "1",
    observedEntries: "1",
    completedDirectories: "1",
    degradedDirectories: "0",
    updatedAt: "2026-09-21T12:00:00Z",
    lastCompletedAt: "2026-09-21T12:00:00Z",
    ...overrides,
  };
}

let liveStatus = liveStatusResponse();

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(480);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(320);
  liveStatus = liveStatusResponse();
  server.use(http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/index-status`, () => HttpResponse.json(liveStatus)));
});

afterEach(() => vi.restoreAllMocks());

async function renderFiles(path = `/files/${FIXTURE_ROOT_ID}`, strict = false) {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
  const contents = (
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <MemoryRouter initialEntries={[path]}>
          <FileNavigationProvider>
            <LocationProbe />
            <HistoryStateProbe />
            <BackButton />
            <FilterSortRouteButton />
            <NavigationRecordProbe />
            <Routes>
              <Route path="/files/:rootId" element={<FilesPage />} />
              <Route path="/files/:rootId/directories/:parentId" element={<FilesPage />} />
            </Routes>
          </FileNavigationProvider>
        </MemoryRouter>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
  render(strict ? <StrictMode>{contents}</StrictMode> : contents);
  return queryClient;
}

describe("FilesPage", () => {
  it("resolves a nested opaque-ID route through the authorized root and directory APIs", async () => {
    let requestedParent = "";
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        requestedParent = new URL(request.url).searchParams.get("parent") ?? "";
        return HttpResponse.json(pageResponse(PARENT_ID));
      }),
    );
    await renderFiles(`/files/${FIXTURE_ROOT_ID}/directories/${PARENT_ID}`);

    expect(await screen.findByRole("heading", {name: "Synthetic archive"})).toBeVisible();
    expect(await screen.findByRole("list", {name: "Files"})).toBeVisible();
    expect(requestedParent).toBe(PARENT_ID);
  });

  it("rejects an unknown root before requesting its directory", async () => {
    let directoryRequests = 0;
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse([]))),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, () => {
        directoryRequests += 1;
        return HttpResponse.json(pageResponse(null));
      }),
    );
    await renderFiles();

    expect(await screen.findByRole("heading", {name: "Root unavailable"})).toBeVisible();
    expect(directoryRequests).toBe(0);
  });

  it("uses opaque directory IDs in routes and shows bounded file details", async () => {
    const directory = {...fixtureEntry(0), id: PARENT_ID, displayName: "Nested", kind: "directory" as const, typeHint: null};
    const file = {...fixtureEntry(1), displayName: "Résumé العائلة.pdf", typeHint: "pdf"};
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const parent = new URL(request.url).searchParams.get("parent");
        return HttpResponse.json(pageResponse(parent, parent ? [] : [directory, file]));
      }),
      http.get(`/api/v1/entries/${file.id}`, () => HttpResponse.json({
        ...file, parentId: FIXTURE_ROOT_ID, ancestors: [], ancestorsTruncated: false,
      })),
    );
    await renderFiles();

    fireEvent.click(await screen.findByRole("button", {name: `Select file ${file.displayName}`}));
    const details = screen.getByRole("region", {name: "File details"});
    await waitFor(() => expect(details).toHaveTextContent(file.displayName));
    expect(screen.queryByRole("button", {name: /^(open file|download|view|edit|delete|rename|move)/i}))
      .not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", {name: "Open directory Nested"}));
    await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent(
      `/files/${FIXTURE_ROOT_ID}/directories/${PARENT_ID}`,
    ));
  });

  it.each([
    ["empty", "ready", "No files in this location."],
    ["degraded", "degraded", "Some files could not be indexed."],
    ["indexing", "scanning", "Indexing this location…"],
  ])("renders the %s state without fabricating a total", async (_name, state, message) => {
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, () =>
        HttpResponse.json(pageResponse(null, [], state)),
      ),
    );
    await renderFiles();

    expect(await screen.findByText(message)).toBeVisible();
    expect(screen.queryByText(/\btotal\b/i)).not.toBeInTheDocument();
  });

  it("shows pending root and directory states without exposing stale file data", async () => {
    let releaseRoots = () => {};
    let releaseDirectory = () => {};
    const rootsGate = new Promise<void>((resolve) => { releaseRoots = resolve; });
    const directoryGate = new Promise<void>((resolve) => { releaseDirectory = resolve; });
    server.use(
      http.get("/api/v1/roots", async () => {
        await rootsGate;
        return HttpResponse.json(rootResponse());
      }),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, async () => {
        await directoryGate;
        return HttpResponse.json(pageResponse(null));
      }),
    );
    await renderFiles();
    expect(screen.getByText("Loading root…")).toHaveAttribute("role", "status");
    releaseRoots();
    expect(await screen.findByRole("heading", {name: "Synthetic archive"})).toBeVisible();
    expect(screen.getByText("Loading files…")).toHaveAttribute("role", "status");
    releaseDirectory();
  });

  it("recreates navigation ownership under StrictMode without duplicate requests", async () => {
    let requests = 0;
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, () => {
        requests += 1;
        return HttpResponse.json(pageResponse(null));
      }),
    );
    await renderFiles(undefined, true);

    expect(await screen.findByRole("list", {name: "Files"})).toBeVisible();
    expect(requests).toBe(1);
  });

  it("restores a visible anchor after navigating into a directory and back", async () => {
    const rootEntries = Array.from({length: 100}, (_, index) => fixtureEntry(index));
    rootEntries[3] = {
      ...rootEntries[3]!,
      id: PARENT_ID,
      displayName: "Nested history directory",
      kind: "directory",
      typeHint: null,
    };
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const parent = new URL(request.url).searchParams.get("parent");
        return HttpResponse.json(pageResponse(parent, parent ? [] : rootEntries));
      }),
    );
    await renderFiles();
    const viewport = await screen.findByTestId("file-list-viewport");
    fireEvent.scroll(viewport, {target: {scrollTop: 160}});
    await waitFor(() => expect(viewport).toHaveAttribute(
      "data-first-visible-id",
      rootEntries[2]!.id,
    ));

    fireEvent.click(screen.getByRole("button", {name: "Open directory Nested history directory"}));
    expect(await screen.findByText("No files in this location.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", {name: "Back in history"}));

    const restoredViewport = await screen.findByTestId("file-list-viewport");
    await waitFor(() => expect(restoredViewport.scrollTop).toBe(160));
    expect(restoredViewport).toHaveAttribute("data-first-visible-id", rootEntries[2]!.id);
  });

  it("changes filter and sort query identity without placing filter text in the route path", async () => {
    const requests: URL[] = [];
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        requests.push(new URL(request.url));
        return HttpResponse.json(pageResponse(null));
      }),
    );
    await renderFiles();
    expect(await screen.findByRole("list", {name: "Files"})).toBeVisible();

    fireEvent.click(screen.getByRole("button", {name: "Change filter and sort route"}));
    await waitFor(() => expect(requests).toHaveLength(2));

    expect(requests[0]!.searchParams.get("sort")).toBe("name");
    expect(requests[1]!.searchParams.get("sort")).toBe("size");
    expect(requests[1]!.searchParams.get("order")).toBe("desc");
    expect(JSON.parse(requests[1]!.searchParams.get("filters")!)).toEqual({v: 1, kind: ["file"]});
    expect(screen.getByLabelText("Current route")).toHaveTextContent(`/files/${FIXTURE_ROOT_ID}`);
    expect(screen.getByLabelText("History state")).toHaveTextContent("null");
  });

  it("withholds filenames during real POP revalidation and restores the bounded anchor", async () => {
    const rootEntries = Array.from({length: 100}, (_, index) => fixtureEntry(index));
    rootEntries[3] = {
      ...rootEntries[3]!,
      id: PARENT_ID,
      displayName: "Nested authenticated directory",
      kind: "directory",
      typeHint: null,
    };
    let sessionCalls = 0;
    let releaseSession = () => {};
    const sessionGate = new Promise<void>((resolve) => { releaseSession = resolve; });
    server.use(
      http.get("/api/v1/auth/session", async () => {
        sessionCalls += 1;
        if (sessionCalls > 1) await sessionGate;
        return HttpResponse.json(SESSION);
      }),
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const parent = new URL(request.url).searchParams.get("parent");
        return HttpResponse.json(pageResponse(parent, parent ? [] : rootEntries));
      }),
    );
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/files/${FIXTURE_ROOT_ID}`]}>
          <BackButton />
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const viewport = await screen.findByTestId("file-list-viewport");
    fireEvent.scroll(viewport, {target: {scrollTop: 160}});
    await waitFor(() => expect(viewport).toHaveAttribute(
      "data-first-visible-id",
      rootEntries[2]!.id,
    ));
    fireEvent.click(screen.getByRole("button", {name: "Open directory Nested authenticated directory"}));
    expect(await screen.findByText("No files in this location.")).toBeVisible();

    fireEvent.click(screen.getByRole("button", {name: "Back in history"}));
    try {
      expect(screen.getByLabelText("Checking session")).toBeVisible();
      expect(screen.queryByText(rootEntries[2]!.displayName)).not.toBeInTheDocument();
      await waitFor(() => expect(sessionCalls).toBe(2));
    } finally {
      releaseSession();
    }

    const restoredViewport = await screen.findByTestId("file-list-viewport");
    await waitFor(() => expect(restoredViewport.scrollTop).toBe(160));
    expect(restoredViewport).toHaveAttribute("data-first-visible-id", rootEntries[2]!.id);
  });

  it("restores a deep-page anchor after POP validation and inactive query eviction", async () => {
    const deepDirectory = {
      ...fixtureEntry(620),
      id: PARENT_ID,
      displayName: "Deep retained directory",
      kind: "directory" as const,
      typeHint: null,
    };
    const requestedCursors: Array<string | null> = [];
    let sessionCalls = 0;
    let releaseSession = () => {};
    const sessionGate = new Promise<void>((resolve) => { releaseSession = resolve; });
    server.use(
      http.get("/api/v1/auth/session", async () => {
        sessionCalls += 1;
        if (sessionCalls > 1) await sessionGate;
        return HttpResponse.json(SESSION);
      }),
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const url = new URL(request.url);
        const parent = url.searchParams.get("parent");
        if (parent) return HttpResponse.json(pageResponse(parent, []));
        const cursor = url.searchParams.get("cursor");
        requestedCursors.push(cursor);
        const pageNumber = cursor === null ? 0 : Number(cursor.split(":").at(-1));
        const pageEntries = Array.from({length: 100}, (_, index) =>
          pageNumber === 6 && index === 20 ? deepDirectory : fixtureEntry(pageNumber * 100 + index),
        );
        return HttpResponse.json({
          ...pageResponse(null, pageEntries),
          nextCursor: pageNumber < 6 ? `cursor:${pageNumber + 1}` : null,
          previousCursor: pageNumber > 0 ? `cursor:${pageNumber - 1}` : null,
        });
      }),
    );
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/files/${FIXTURE_ROOT_ID}`]}>
          <BackButton />
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const viewport = await screen.findByTestId("file-list-viewport");
    Object.defineProperty(viewport, "clientHeight", {configurable: true, value: 480});
    for (let pageNumber = 1; pageNumber <= 6; pageNumber += 1) {
      fireEvent.click(screen.getByRole("button", {name: "Load more files"}));
      await waitFor(() => expect(requestedCursors.at(-1)).toBe(`cursor:${pageNumber}`));
      if (pageNumber < 6) {
        await waitFor(() => expect(screen.getByRole("button", {name: "Load more files"})).toBeEnabled());
      }
    }
    fireEvent.scroll(viewport, {target: {scrollTop: 420 * 80}});
    await waitFor(() => expect(viewport).toHaveAttribute("data-first-visible-id", PARENT_ID));
    fireEvent.click(screen.getByRole("button", {name: "Open directory Deep retained directory"}));
    expect(await screen.findByText("No files in this location.")).toBeVisible();

    const rootDirectoryKey = directoryQueryOptions({
      namespace: SESSION.cacheNamespace,
      rootId: FIXTURE_ROOT_ID,
      rootEpoch: 4,
      parentId: null,
      filters: {v: 1},
      sort: "name",
      order: "asc",
      cursor: null,
    }).queryKey;
    await waitFor(() => expect(queryClient.getQueryData(rootDirectoryKey)).toBeUndefined());

    fireEvent.click(screen.getByRole("button", {name: "Back in history"}));
    try {
      expect(screen.getByLabelText("Checking session")).toBeVisible();
      expect(screen.queryByText("Deep retained directory")).not.toBeInTheDocument();
      await waitFor(() => expect(sessionCalls).toBe(2));
    } finally {
      releaseSession();
    }

    const restoredViewport = await screen.findByTestId("file-list-viewport");
    await waitFor(() => expect(requestedCursors.at(-1)).toBe("cursor:6"));
    await waitFor(() => expect(restoredViewport.scrollTop).toBe(20 * 80));
    expect(restoredViewport).toHaveAttribute("data-first-visible-id", PARENT_ID);
  });

  it("keeps drafts out of the query, applies once, resets the cursor and restores focus", async () => {
    const requests: URL[] = [];
    const file = fixtureEntry(1);
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        requests.push(new URL(request.url));
        return HttpResponse.json(pageResponse(null, [file]));
      }),
      http.get(`/api/v1/entries/${file.id}`, () => HttpResponse.json({
        ...file, parentId: FIXTURE_ROOT_ID, ancestors: [], ancestorsTruncated: false,
      })),
    );
    await renderFiles();
    fireEvent.click(await screen.findByRole("button", {name: `Select file ${file.displayName}`}));
    expect(screen.getByRole("region", {name: "File details"})).toBeVisible();
    const filtersButton = screen.getByRole("button", {name: "Filters"});

    filtersButton.focus();
    fireEvent.click(filtersButton);
    fireEvent.change(screen.getByLabelText("Filename starts with"), {target: {value: "IMG_"}});
    fireEvent.click(screen.getByRole("button", {name: "Cancel"}));
    expect(filtersButton).toHaveFocus();
    fireEvent.click(filtersButton);
    expect(screen.getByLabelText("Filename starts with")).toHaveValue("");
    fireEvent.keyDown(screen.getByRole("dialog"), {key: "Escape"});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(filtersButton).toHaveFocus();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(requests).toHaveLength(1);
    expect(screen.getByRole("region", {name: "File details"})).toBeVisible();

    fireEvent.click(filtersButton);
    fireEvent.click(screen.getByRole("checkbox", {name: "File"}));
    fireEvent.change(screen.getByLabelText("Filename starts with"), {target: {value: "IMG_"}});
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    expect(filtersButton).toHaveFocus();
    expect(screen.queryByRole("region", {name: "File details"})).not.toBeInTheDocument();
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(JSON.parse(requests[1]!.searchParams.get("filters")!)).toEqual({v: 1, kind: ["file"], prefix: "IMG_"});
    expect(requests[1]!.searchParams.has("cursor")).toBe(false);
    expect(screen.getByLabelText("Current route")).toHaveTextContent(`/files/${FIXTURE_ROOT_ID}`);
    expect(screen.getByLabelText("History state")).toHaveTextContent("null");
    await waitFor(() => expect(screen.getByTestId("files-announcement")).toHaveTextContent("Filters applied."));
    expect(screen.getByTestId("files-announcement")).not.toHaveTextContent(/\d/);

    fireEvent.click(screen.getByRole("button", {name: "Remove filter Kind: File"}));
    await waitFor(() => expect(requests).toHaveLength(3));
    expect(JSON.parse(requests[2]!.searchParams.get("filters")!)).toEqual({v: 1, prefix: "IMG_"});
    fireEvent.click(await screen.findByRole("button", {name: "Clear all filters"}));
    await waitFor(() => expect(requests).toHaveLength(4));
    expect(JSON.parse(requests[3]!.searchParams.get("filters")!)).toEqual({v: 1});
    expect(screen.queryByRole("button", {name: /^Remove filter/})).not.toBeInTheDocument();
  });

  it("restarts at page one after applying filters from a deep restored cursor", async () => {
    const requests: URL[] = [];
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const url = new URL(request.url);
        requests.push(url);
        const cursor = url.searchParams.get("cursor");
        const pageNumber = cursor === null ? 0 : Number(cursor.split(":").at(-1));
        return HttpResponse.json({
          ...pageResponse(null, Array.from({length: 100}, (_, index) => fixtureEntry(pageNumber * 100 + index))),
          nextCursor: `cursor:${pageNumber + 1}`,
          previousCursor: pageNumber > 0 ? `cursor:${pageNumber - 1}` : null,
        });
      }),
    );
    await renderFiles();
    const viewport = await screen.findByTestId("file-list-viewport");
    fireEvent.click(screen.getByRole("button", {name: "Load more files"}));
    await waitFor(() => expect(requests.at(-1)!.searchParams.get("cursor")).toBe("cursor:1"));
    fireEvent.scroll(viewport, {target: {scrollTop: 150 * 80}});
    fireEvent.click(screen.getByRole("button", {name: "Filters"}));
    fireEvent.click(screen.getByRole("checkbox", {name: "No extension (unknown type)"}));
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    await waitFor(() => expect(JSON.parse(requests.at(-1)!.searchParams.get("filters")!))
      .toEqual({v: 1, type: ["__unknown__"]}));
    expect(requests.at(-1)!.searchParams.has("cursor")).toBe(false);
    const filteredViewport = await screen.findByTestId("file-list-viewport");
    expect(filteredViewport.scrollTop).toBe(0);
    expect(filteredViewport).toHaveAttribute("data-first-visible-id", fixtureEntry(0).id);
  });

  it("distinguishes a zero-match filter result from an unindexed directory", async () => {
    let state = "ready";
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const filters = JSON.parse(new URL(request.url).searchParams.get("filters")!);
        return HttpResponse.json(pageResponse(null, Object.keys(filters).length > 1 ? [] : [fixtureEntry(0)], state));
      }),
    );
    await renderFiles();
    await screen.findByRole("list", {name: "Files"});
    fireEvent.click(screen.getByRole("button", {name: "Filters"}));
    fireEvent.click(screen.getByRole("checkbox", {name: "Symbolic link"}));
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    expect(await screen.findByText("No entries match these filters.")).toBeVisible();
    expect(screen.queryByText("No files in this location.")).not.toBeInTheDocument();
    expect(screen.queryByText(/not been indexed/i)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("files-announcement"))
      .toHaveTextContent("No entries match these filters."));

    state = "not_indexed";
    fireEvent.click(screen.getByRole("button", {name: "Remove filter Kind: Symbolic link"}));
    fireEvent.click(screen.getByRole("button", {name: "Filters"}));
    fireEvent.click(screen.getByRole("checkbox", {name: "Special entry"}));
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    expect(await screen.findByText("This location has not been indexed yet.")).toBeVisible();
    expect(screen.queryByText("No entries match these filters.")).not.toBeInTheDocument();
  });

  it.each([
    [["browse", "preview", "export", "create", "organize", "copy", "delete_restore"], false],
    [["browse", "root_admin"], true],
  ])("shows rescan only for the root_admin capability (%j)", async (permissions, visible) => {
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse([{
        id: FIXTURE_ROOT_ID,
        displayName: "Synthetic archive",
        mode: "read_only",
        permissions,
        authorizationEpoch: 4,
      }]))),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, () => HttpResponse.json(pageResponse(null))),
    );
    await renderFiles();
    expect(await screen.findByText("Index ready")).toBeVisible();
    expect(Boolean(screen.queryByRole("button", {name: "Request rescan"}))).toBe(visible);
    expect(screen.queryByRole("button", {name: /delete|rename|move|thumbnail/i})).not.toBeInTheDocument();
  });

  it("cancels and discards selected-file details after directory navigation", async () => {
    const directory = {...fixtureEntry(0), id: PARENT_ID, displayName: "Nested", kind: "directory" as const, typeHint: null};
    const file = {...fixtureEntry(1), displayName: "Stale private.pdf", typeHint: "pdf"};
    let detailsSignal: AbortSignal | undefined;
    let releaseDetails = () => {};
    const detailsGate = new Promise<void>((resolve) => { releaseDetails = resolve; });
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const parent = new URL(request.url).searchParams.get("parent");
        return HttpResponse.json(pageResponse(parent, parent ? [] : [directory, file]));
      }),
      http.get(`/api/v1/entries/${file.id}`, async ({request}) => {
        detailsSignal = request.signal;
        await detailsGate;
        return HttpResponse.json({...file, parentId: FIXTURE_ROOT_ID, ancestors: [], ancestorsTruncated: false});
      }),
    );
    await renderFiles();
    fireEvent.click(await screen.findByRole("button", {name: `Select file ${file.displayName}`}));
    await waitFor(() => expect(detailsSignal).toBeDefined());
    fireEvent.click(screen.getByRole("button", {name: "Open directory Nested"}));
    expect(await screen.findByText("No files in this location.")).toBeVisible();
    await waitFor(() => expect(detailsSignal?.aborted).toBe(true));
    releaseDetails();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("region", {name: "File details"})).not.toBeInTheDocument();
    expect(screen.queryByText("Stale private.pdf")).not.toBeInTheDocument();
  });

  it("retries an empty first page on scan progress at most once per active interval", async () => {
    let directoryCalls = 0;
    let rows: ReturnType<typeof fixtureEntry>[] = [];
    liveStatus = liveStatusResponse({state: "scanning", observedEntries: "0", lastCompletedAt: null});
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, () => {
        directoryCalls += 1;
        return HttpResponse.json(pageResponse(null, rows, "scanning"));
      }),
    );
    const queryClient = await renderFiles();
    expect(await screen.findByText("Indexing in progress")).toBeVisible();
    expect(screen.getByText("Indexing this location…")).toBeVisible();
    expect(directoryCalls).toBe(1);
    const statusKey = ["files", SESSION.cacheNamespace, "index-status"];

    liveStatus = liveStatusResponse({state: "scanning", observedEntries: "5", lastCompletedAt: null});
    await queryClient.refetchQueries({queryKey: statusKey});
    await waitFor(() => expect(directoryCalls).toBe(2));

    liveStatus = liveStatusResponse({state: "scanning", observedEntries: "9", lastCompletedAt: null});
    rows = [fixtureEntry(0)];
    await queryClient.refetchQueries({queryKey: statusKey});
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(directoryCalls).toBe(2);
    expect(screen.queryByRole("list", {name: "Files"})).not.toBeInTheDocument();
  });

  it("offers Refresh for a new catalog generation instead of refetching a visible window", async () => {
    let directoryCalls = 0;
    let generation = "1";
    const rootEntries = Array.from({length: 100}, (_, index) => fixtureEntry(index));
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, () => {
        directoryCalls += 1;
        const page = pageResponse(null, rootEntries);
        return HttpResponse.json({...page, indexStatus: {...page.indexStatus, generation}});
      }),
    );
    const queryClient = await renderFiles();
    const viewport = await screen.findByTestId("file-list-viewport");
    await screen.findByText("Index ready");
    fireEvent.scroll(viewport, {target: {scrollTop: 400}});
    await waitFor(() => expect(viewport).toHaveAttribute("data-first-visible-id", rootEntries[5]!.id));

    generation = "2";
    liveStatus = liveStatusResponse({generation: "2"});
    await queryClient.refetchQueries({queryKey: ["files", SESSION.cacheNamespace, "index-status"]});
    const refresh = await screen.findByRole("button", {name: "Refresh files"});
    expect(directoryCalls).toBe(1);
    fireEvent.click(refresh);
    await waitFor(() => expect(directoryCalls).toBe(2));
    await waitFor(() => expect(screen.queryByRole("button", {name: "Refresh files"})).not.toBeInTheDocument());
    expect(screen.getByTestId("file-list-viewport")).toHaveAttribute("data-first-visible-id", rootEntries[5]!.id);
  });

  it("keeps the applied filters as the route record while page one loads", async () => {
    let releaseFiltered = () => {};
    const filteredGate = new Promise<void>((resolve) => { releaseFiltered = resolve; });
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, async ({request}) => {
        const url = new URL(request.url);
        const filtered = Object.keys(JSON.parse(url.searchParams.get("filters")!)).length > 1;
        if (filtered) await filteredGate;
        const cursor = url.searchParams.get("cursor");
        const pageNumber = cursor === null ? 0 : Number(cursor.split(":").at(-1));
        return HttpResponse.json({
          ...pageResponse(null, Array.from({length: 100}, (_, index) => fixtureEntry(pageNumber * 100 + index))),
          nextCursor: `cursor:${pageNumber + 1}`,
          previousCursor: pageNumber > 0 ? `cursor:${pageNumber - 1}` : null,
        });
      }),
    );
    await renderFiles();
    const viewport = await screen.findByTestId("file-list-viewport");
    fireEvent.click(screen.getByRole("button", {name: "Load more files"}));
    await waitFor(() => expect(screen.getByRole("button", {name: "Load more files"})).toBeEnabled());
    fireEvent.scroll(viewport, {target: {scrollTop: 150 * 80}});
    fireEvent.click(screen.getByRole("button", {name: "Filters"}));
    fireEvent.change(screen.getByLabelText("Filename starts with"), {target: {value: "Synthetic"}});
    fireEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    expect(await screen.findByText("Loading files…")).toBeVisible();

    const probe = screen.getByRole("button", {name: "Inspect navigation record"});
    try {
      fireEvent.click(probe);
      expect(JSON.parse(probe.dataset.record!)).toEqual({
        filters: {v: 1, prefix: "Synthetic"}, cursor: null, anchor: null,
      });
    } finally {
      releaseFiltered();
    }
    await screen.findByTestId("file-list-viewport");
  });

  it("does not resurrect a closed-by-navigation detail on Back", async () => {
    const directory = {...fixtureEntry(0), id: PARENT_ID, displayName: "Nested", kind: "directory" as const, typeHint: null};
    const file = {...fixtureEntry(1), displayName: "Private detail.pdf", typeHint: "pdf"};
    let detailRequests = 0;
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const parent = new URL(request.url).searchParams.get("parent");
        return HttpResponse.json(pageResponse(parent, parent === PARENT_ID ? [] : [directory, file]));
      }),
      http.get(`/api/v1/entries/${file.id}`, () => {
        detailRequests += 1;
        return HttpResponse.json({...file, parentId: FIXTURE_ROOT_ID, ancestors: [], ancestorsTruncated: false});
      }),
    );
    // Start in a directory route so directory navigation reuses the same FilesPage instance.
    await renderFiles(`/files/${FIXTURE_ROOT_ID}/directories/33333333-3333-4333-8333-333333333333`);
    fireEvent.click(await screen.findByRole("button", {name: `Select file ${file.displayName}`}));
    await waitFor(() => expect(screen.getByRole("region", {name: "File details"}))
      .toHaveTextContent(file.displayName));
    expect(detailRequests).toBe(1);

    fireEvent.click(screen.getByRole("button", {name: "Open directory Nested"}));
    expect(await screen.findByText("No files in this location.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", {name: "Back in history"}));
    expect(await screen.findByRole("button", {name: `Select file ${file.displayName}`})).toBeVisible();
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.queryByRole("region", {name: "File details"})).not.toBeInTheDocument();
    expect(detailRequests).toBe(1);
  });
});
