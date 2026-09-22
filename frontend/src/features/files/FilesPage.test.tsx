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

function FilterSortRouteButton() {
  return (
    <FileNavigationRouteConsumer>
      {({navigation, rememberRoute}) => (
        <FilterSortRouteControl navigation={navigation} rememberRoute={rememberRoute} />
      )}
    </FileNavigationRouteConsumer>
  );
}

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(480);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(320);
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

  it("uses opaque directory IDs in routes and shows a bounded inline file summary", async () => {
    const directory = {...fixtureEntry(0), id: PARENT_ID, displayName: "Nested", kind: "directory" as const, typeHint: null};
    const file = {...fixtureEntry(1), displayName: "Résumé العائلة.pdf", typeHint: "pdf"};
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json(rootResponse())),
      http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/entries`, ({request}) => {
        const parent = new URL(request.url).searchParams.get("parent");
        return HttpResponse.json(pageResponse(parent, parent ? [] : [directory, file]));
      }),
    );
    await renderFiles();

    fireEvent.click(await screen.findByRole("button", {name: `Select file ${file.displayName}`}));
    expect(screen.getByRole("region", {name: "Selected file"})).toHaveTextContent(file.displayName);
    expect(screen.queryByRole("button", {name: /^(open file|download)/i})).not.toBeInTheDocument();

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
});
