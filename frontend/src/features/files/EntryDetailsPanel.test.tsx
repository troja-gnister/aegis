import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {useState} from "react";
import {afterEach, describe, expect, it} from "vitest";
import {server} from "../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {AuthSessionContext, isSessionAccessOpen} from "../auth/session";
import type {SessionResponse} from "../auth/types";
import {EntryDetailsPanel} from "./EntryDetailsPanel";
import {createFileNavigation} from "./navigation";
import {FIXTURE_ROOT_ID} from "./test-fixtures";

const ENTRY_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_ID = "44444444-4444-4444-8444-444444444444";
const PARENT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION: SessionResponse = {
  user: {id: "1797b1eb-c642-4f66-99a4-a878347b4e49", username: "alice"},
  cacheNamespace: "entry-details-namespace",
};
const clients = new Set<QueryClient>();

function details(overrides: Record<string, unknown> = {}) {
  return {
    id: ENTRY_ID,
    rootId: FIXTURE_ROOT_ID,
    displayName: "Holiday album.jpg",
    kind: "file",
    typeHint: "jpg",
    size: "18446744073709551615",
    modifiedNs: "1799999999123456789",
    sourceState: "present",
    version: "9223372036854775807",
    parentId: PARENT_ID,
    ancestors: [
      {id: FIXTURE_ROOT_ID, displayName: ""},
      {id: PARENT_ID, displayName: "Trips 2027"},
    ],
    ancestorsTruncated: false,
    ...overrides,
  };
}

async function renderPanel(entryId = ENTRY_ID, onClose = () => undefined) {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
  const view = render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <EntryDetailsPanel entryId={entryId} onClose={onClose} />
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
  return {queryClient, ...view};
}

afterEach(async () => {
  await Promise.all([...clients].map((client) => purgePrivateBrowserState(client)));
  clients.clear();
});

describe("EntryDetailsPanel", () => {
  it("shows exact BigInt metadata, file-modified time, state, version and bounded ancestors", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(details())));
    await renderPanel();

    const panel = await screen.findByRole("region", {name: "File details"});
    expect(await screen.findByRole("heading", {name: "Holiday album.jpg"})).toBeVisible();
    expect(panel).toHaveTextContent("18,446,744,073,709,551,615 bytes");
    expect(panel).toHaveTextContent("about 18.4 EB");
    expect(panel).toHaveTextContent("2027-01-15T07:59:59.123456789Z");
    expect(panel).toHaveTextContent(/not a capture time/i);
    expect(panel).toHaveTextContent("9,223,372,036,854,775,807");
    expect(panel).toHaveTextContent(".jpg");
    expect(panel).toHaveTextContent("Present when last indexed");
    const location = screen.getByRole("list", {name: "Location"});
    expect(location).toHaveTextContent("Root");
    expect(location).toHaveTextContent("Trips 2027");
    expect(panel).not.toHaveTextContent(/earlier folders omitted/i);
  });

  it("marks truncated ancestry and labels an unknown type without inventing one", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(details({
      typeHint: null, size: null, modifiedNs: null, ancestorsTruncated: true,
    }))));
    await renderPanel();
    const panel = await screen.findByRole("region", {name: "File details"});
    await waitFor(() => expect(panel).toHaveTextContent(/earlier folders omitted/i));
    expect(panel).toHaveTextContent("No extension");
    expect(panel).toHaveTextContent("Size unknown");
    expect(panel).toHaveTextContent("Modified time unknown");
  });

  it.each([
    ["symlink", "present", /symbolic link.*never followed/i],
    ["special", "present", /special filesystem entry.*metadata only/i],
    ["file", "unsupported", /not supported/i],
    ["file", "inaccessible", /one of its folders is currently inaccessible/i],
    ["file", "missing", /missing when last indexed\. its last indexed metadata is kept/i],
  ])("explains %s entries in source state %s as informational", async (kind, sourceState, message) => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () =>
      HttpResponse.json(details({kind, sourceState})),
    ));
    await renderPanel();
    expect(await screen.findByText(message)).toBeVisible();
  });

  it("exposes only a close control, never a viewer, editor, download or destructive action", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(details())));
    await renderPanel();
    await screen.findByRole("heading", {name: "Holiday album.jpg"});
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["Close details"]);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByText(/thumbnail/i)).not.toBeInTheDocument();
  });

  it("cancels the in-flight details request when closed", async () => {
    let signal: AbortSignal | undefined;
    let release = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, async ({request}) => {
      signal = request.signal;
      await gate;
      return HttpResponse.json(details());
    }));
    function Harness() {
      const [open, setOpen] = useState(true);
      return open ? <EntryDetailsPanel entryId={ENTRY_ID} onClose={() => setOpen(false)} /> : <p>Closed</p>;
    }
    const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
    clients.add(queryClient);
    await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
    render(
      <QueryClientProvider client={queryClient}>
        <AuthSessionContext.Provider value={{session: SESSION}}>
          <Harness />
        </AuthSessionContext.Provider>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading file details…");
    await waitFor(() => expect(signal).toBeDefined());
    fireEvent.click(screen.getByRole("button", {name: "Close details"}));
    expect(screen.getByText("Closed")).toBeVisible();
    await waitFor(() => expect(signal?.aborted).toBe(true));
    release();
    await waitFor(() => expect(
      queryClient.getQueryCache().findAll({queryKey: ["files", SESSION.cacheNamespace, "entry"]}),
    ).toHaveLength(0));
  });

  it("never shows a previous entry's details for a newly selected entry", async () => {
    let releaseFirst = () => {};
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    server.use(
      http.get(`/api/v1/entries/${ENTRY_ID}`, async () => {
        await firstGate;
        return HttpResponse.json(details());
      }),
      http.get(`/api/v1/entries/${OTHER_ID}`, () =>
        HttpResponse.json(details({id: OTHER_ID, displayName: "Second.pdf", typeHint: "pdf"})),
      ),
    );
    const {rerender, queryClient} = await renderPanel();
    rerender(
      <QueryClientProvider client={queryClient}>
        <AuthSessionContext.Provider value={{session: SESSION}}>
          <EntryDetailsPanel entryId={OTHER_ID} onClose={() => undefined} />
        </AuthSessionContext.Provider>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("heading", {name: "Second.pdf"})).toBeVisible();
    releaseFirst();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("Holiday album.jpg")).not.toBeInTheDocument();
  });

  it("distinguishes an entry that left the index from a failed request", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () =>
      HttpResponse.json({type: "catalog_not_found", title: "Catalog item not found"}, {status: 404}),
    ));
    await renderPanel();
    expect(await screen.findByText(/no longer in the index/i)).toBeVisible();
  });

  it("offers a retry after a failed details request", async () => {
    let calls = 0;
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => {
      calls += 1;
      return calls === 1
        ? HttpResponse.json({type: "catalog_unavailable", title: "Catalog unavailable"}, {status: 503})
        : HttpResponse.json(details());
    }));
    await renderPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent("File details could not be loaded.");
    fireEvent.click(screen.getByRole("button", {name: "Retry details"}));
    expect(await screen.findByRole("heading", {name: "Holiday album.jpg"})).toBeVisible();
  });

  it("closes private access and purges navigation on a 401", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () =>
      HttpResponse.json({type: "authentication_required", title: "Authentication required"}, {status: 401}),
    ));
    const navigation = createFileNavigation(SESSION.cacheNamespace);
    navigation.remember("key", {
      rootId: FIXTURE_ROOT_ID, parentId: null, filters: {v: 1, prefix: "private"}, sort: "name",
      order: "asc", cursor: null, visibleAnchorId: null, visibleAnchorOffset: 0,
    });
    await renderPanel();
    await waitFor(() => expect(isSessionAccessOpen()).toBe(false));
    expect(navigation.size).toBe(0);
    expect(screen.queryByText("Holiday album.jpg")).not.toBeInTheDocument();
    navigation.dispose();
  });
});
