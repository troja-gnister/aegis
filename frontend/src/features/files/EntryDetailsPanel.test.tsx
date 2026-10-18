import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {useState} from "react";
import {MemoryRouter, Route, Routes, useLocation} from "react-router";
import {afterEach, describe, expect, it, vi} from "vitest";
import {server} from "../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {AuthSessionContext, isSessionAccessOpen} from "../auth/session";
import type {SessionResponse} from "../auth/types";
import {derivativeUrlFor, entryMediaFixture, mediaDetailsBody, originalUrlFor} from "../media/test-fixtures";
import {EntryDetailsPanel, type EntryDetailsPanelProps} from "./EntryDetailsPanel";
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
    media: null,
    downloadUrl: null,
    parentId: PARENT_ID,
    ancestors: [
      {id: FIXTURE_ROOT_ID, displayName: ""},
      {id: PARENT_ID, displayName: "Trips 2027"},
    ],
    ancestorsTruncated: false,
    ...overrides,
  };
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

type PanelOptions = Partial<Omit<EntryDetailsPanelProps, "entryId">>;

const OPENER_CONTEXT = {
  source: "directory", rootId: FIXTURE_ROOT_ID, parentId: PARENT_ID, returnKey: "opener0",
} as const;

function panelTree(queryClient: QueryClient, entryId: string, options: PanelOptions = {}) {
  const props: PanelOptions = {viewerContext: OPENER_CONTEXT, ...options};
  return (
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <MemoryRouter initialEntries={[`/files/${FIXTURE_ROOT_ID}/directories/${PARENT_ID}`]}>
          <Routes>
            <Route
              path="*"
              element={(
                <>
                  <EntryDetailsPanel entryId={entryId} onClose={() => undefined} {...props} />
                  <LocationProbe />
                </>
              )}
            />
          </Routes>
        </MemoryRouter>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

async function renderPanel(entryId = ENTRY_ID, options: PanelOptions = {}) {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
  const view = render(panelTree(queryClient, entryId, options));
  return {queryClient, ...view};
}

/** A described photo with EXPORT: the entry details carry its attachment URL. */
function mediaEntry(overrides: Record<string, unknown> = {}) {
  return details({
    media: entryMediaFixture(ENTRY_ID), downloadUrl: originalUrlFor(ENTRY_ID, "attachment"), ...overrides,
  });
}

function serveMedia(entry: Record<string, unknown>, media: Record<string, unknown> | (() => Response)) {
  server.use(
    http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(entry)),
    http.get(`/api/v1/entries/${ENTRY_ID}/media`, () =>
      typeof media === "function" ? media() : HttpResponse.json(media),
    ),
  );
}

function descriptionFor(term: string): HTMLElement {
  const label = screen.getByText(term, {selector: "dt"});
  return label.nextElementSibling as HTMLElement;
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

  it("exposes only a close control without EXPORT, never a viewer, editor or destructive action", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(details())));
    await renderPanel();
    await screen.findByRole("heading", {name: "Holiday album.jpg"});
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["Close details"]);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByText(/thumbnail/i)).not.toBeInTheDocument();
    // A .jpg the media catalog has not described yet.
    expect(screen.getByText(
      "Originals remain read only. Opening and preview are not available for this file yet.",
    )).toBeVisible();
    expect(screen.getByText("Your access to this file does not include downloading it.")).toBeVisible();
    expect(screen.queryByText(/not available in this version/i)).not.toBeInTheDocument();
  });

  it("keeps the version note for files that are not photos or videos", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () =>
      HttpResponse.json(details({displayName: "Notes.pdf", typeHint: "pdf"})),
    ));
    await renderPanel();
    await screen.findByRole("heading", {name: "Notes.pdf"});
    expect(screen.getByText("Originals remain read only. Opening and preview are not available in this version."))
      .toBeVisible();
  });

  it("offers Download original for any file with a download URL, as a same-origin attachment link", async () => {
    let mediaRequests = 0;
    server.use(
      http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(details({
        displayName: "Quarterly report.pdf", typeHint: "pdf", downloadUrl: originalUrlFor(ENTRY_ID, "attachment"),
      }))),
      http.get(`/api/v1/entries/${ENTRY_ID}/media`, () => {
        mediaRequests += 1;
        return HttpResponse.json(mediaDetailsBody(ENTRY_ID));
      }),
    );
    await renderPanel();

    const download = await screen.findByRole("link", {name: "Download original"});
    expect(download).toHaveAttribute("href", originalUrlFor(ENTRY_ID, "attachment"));
    expect(download).toHaveAttribute("download", "");
    expect(download.getAttribute("href")).not.toMatch(/^(blob:|data:|https?:)/);
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.getByText("Originals remain read only. Opening and preview are not available in this version."))
      .toBeVisible();
    expect(screen.queryByText(/does not include downloading/i)).not.toBeInTheDocument();
    // A non-media file needs no second request for its download.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mediaRequests).toBe(0);
  });

  it("offers Download for a photo the media catalog has not described yet", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () =>
      HttpResponse.json(details({downloadUrl: originalUrlFor(ENTRY_ID, "attachment")})),
    ));
    await renderPanel();
    expect(await screen.findByRole("link", {name: "Download original"})).toHaveAttribute(
      "href", originalUrlFor(ENTRY_ID, "attachment"),
    );
    expect(screen.getByText(
      "Originals remain read only. Opening and preview are not available for this file yet.",
    )).toBeVisible();
  });

  it.each([
    ["directory", "present", /opening, preview and download are not available in this version/i],
    ["symlink", "present", /never followed/i],
    ["special", "present", /metadata only/i],
    ["file", "missing", /missing when last indexed/i],
    ["file", "inaccessible", /currently inaccessible/i],
    ["file", "unsupported", /not supported for content access/i],
  ])("offers no Download and no access note for a %s entry in state %s", async (kind, sourceState, explanation) => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, () =>
      HttpResponse.json(details({kind, sourceState, displayName: "Unavailable entry", typeHint: null})),
    ));
    await renderPanel();
    await screen.findByRole("heading", {name: "Unavailable entry"});
    expect(screen.getAllByText(explanation)[0]).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    // The state explains the missing Download; it is not a permission matter.
    expect(screen.queryByText(/does not include downloading/i)).not.toBeInTheDocument();
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
    rerender(panelTree(queryClient, OTHER_ID));
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
      rootId: FIXTURE_ROOT_ID, parentId: null, filters: {v: 1, prefix: "private"}, scope: "directory", sort: "name",
      order: "asc", cursor: null, visibleAnchorId: null, visibleAnchorOffset: 0,
    });
    await renderPanel();
    await waitFor(() => expect(isSessionAccessOpen()).toBe(false));
    expect(navigation.size).toBe(0);
    expect(screen.queryByText("Holiday album.jpg")).not.toBeInTheDocument();
    navigation.dispose();
  });
});

describe("EntryDetailsPanel media details", () => {
  it("labels a recorded capture time as Captured and keeps it apart from the file modified time", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID, {
      metadata: {capturedAt: "2026-12-24T18:30:00+00:00", capturedAtSource: "exif"},
    }));
    await renderPanel();

    const section = await screen.findByRole("region", {name: "Photo details"});
    await within(section).findByText("Captured", {selector: "dt"});
    const captured = descriptionFor("Captured");
    expect(captured.querySelector("time")).toHaveAttribute("dateTime", "2026-12-24T18:30:00+00:00");
    // A wall-clock capture time is the camera's clock reading: no zone is claimed or converted.
    expect(captured.querySelector("time")).toHaveTextContent(new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium", timeStyle: "medium", timeZone: "UTC",
    }).format(Date.UTC(2026, 11, 24, 18, 30, 0)));
    expect(captured).toHaveTextContent("Exact: 2026-12-24T18:30:00");
    expect(captured).not.toHaveTextContent("+00:00");
    expect(captured).not.toHaveTextContent("(UTC)");
    expect(captured).toHaveTextContent("As recorded by the camera, without a time zone.");
    expect(captured).toHaveTextContent(/EXIF metadata/);
    expect(captured).not.toHaveTextContent("2027-01-15");
    const modified = descriptionFor("File modified");
    expect(modified).toHaveTextContent("2027-01-15T07:59:59.123456789Z");
    expect(modified).toHaveTextContent(/not a capture time/i);
    expect(modified).not.toHaveTextContent("2026-12-24");
    expect(descriptionFor("Media")).toHaveTextContent("Photo");
    expect(descriptionFor("Dimensions")).toHaveTextContent("4,032 × 3,024 pixels");
    expect(descriptionFor("Camera")).toHaveTextContent("Synthetic Optics Model 7");
    expect(within(section).queryByText("Duration", {selector: "dt"})).not.toBeInTheDocument();
  });

  it("never fills a missing capture time from the file modified time", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID, {
      metadata: {capturedAt: null, capturedAtSource: null, cameraMake: null, cameraModel: null},
    }));
    await renderPanel();

    const section = await screen.findByRole("region", {name: "Photo details"});
    await within(section).findByText("Captured", {selector: "dt"});
    const captured = descriptionFor("Captured");
    expect(captured).toHaveTextContent("Capture time unknown");
    expect(captured.querySelector("time")).toBeNull();
    expect(captured).not.toHaveTextContent("2027");
    expect(within(section).queryByText("Camera", {selector: "dt"})).not.toBeInTheDocument();
  });

  it("shows a capture time in its recorded offset rather than the viewer's zone", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID, {
      metadata: {capturedAt: "2026-12-24T18:30:00+09:00", capturedAtSource: "exif"},
    }));
    await renderPanel();
    await screen.findByText("Captured", {selector: "dt"});
    const wallClock = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium", timeStyle: "medium", timeZone: "UTC",
    }).format(Date.UTC(2026, 11, 24, 18, 30, 0));
    expect(descriptionFor("Captured").querySelector("time")).toHaveTextContent(`${wallClock} (UTC+09:00)`);
  });

  it("shows the reported display dimensions without swapping them for the orientation", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID, {
      metadata: {width: 3024, height: 4032, orientation: 6},
    }));
    await renderPanel();
    await screen.findByText("Dimensions", {selector: "dt"});
    expect(descriptionFor("Dimensions")).toHaveTextContent("3,024 × 4,032 pixels");
  });

  it("shows a video's duration and container capture source", async () => {
    serveMedia(
      mediaEntry({
        displayName: "Synthetic clip.mp4",
        typeHint: "mp4",
        media: entryMediaFixture(ENTRY_ID, {kind: "video", durationMs: 3_723_000}),
      }),
      mediaDetailsBody(ENTRY_ID, {kind: "video"}),
    );
    await renderPanel();

    const section = await screen.findByRole("region", {name: "Video details"});
    await within(section).findByText("Duration", {selector: "dt"});
    expect(descriptionFor("Duration")).toHaveTextContent("1:02:03");
    expect(descriptionFor("Captured")).toHaveTextContent("Recorded in the video container.");
    expect(within(section).queryByText("Camera", {selector: "dt"})).not.toBeInTheDocument();
  });

  it("offers one Download, from the entry details, as an attachment link only with EXPORT", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID, {permissions: {preview: true, export: true}}));
    const {unmount} = await renderPanel();

    await screen.findByRole("link", {name: "Open photo"});
    // The media details carry the same URL; the panel shows it once.
    const downloads = screen.getAllByRole("link", {name: "Download original"});
    expect(downloads).toHaveLength(1);
    expect(downloads[0]).toHaveAttribute("href", originalUrlFor(ENTRY_ID, "attachment"));
    expect(downloads[0]).toHaveAttribute("download");
    expect(downloads[0]!.getAttribute("href")).not.toMatch(/^blob:/);
    unmount();

    serveMedia(
      mediaEntry({downloadUrl: null}),
      mediaDetailsBody(ENTRY_ID, {permissions: {preview: true, export: false}}),
    );
    await renderPanel();
    await screen.findByRole("link", {name: "Open photo"});
    expect(screen.queryByRole("link", {name: "Download original"})).not.toBeInTheDocument();
  });

  it("keeps Download when the media details cannot be loaded", async () => {
    serveMedia(
      mediaEntry(),
      () => HttpResponse.json({type: "catalog_not_found", title: "Catalog item not found"}, {status: 404}),
    );
    await renderPanel();
    expect(await screen.findByText("Media details are not available for this file.")).toBeVisible();
    expect(screen.getByRole("link", {name: "Download original"})).toHaveAttribute(
      "href", originalUrlFor(ENTRY_ID, "attachment"),
    );
  });

  it("opens the viewer route with opaque context in history state only with PREVIEW", async () => {
    const onOpenMedia = vi.fn();
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID));
    await renderPanel(ENTRY_ID, {onOpenMedia});

    fireEvent.click(await screen.findByRole("link", {name: "Open photo"}));

    expect(onOpenMedia).toHaveBeenCalledExactlyOnceWith(ENTRY_ID);
    expect(screen.getByLabelText("Current route")).toHaveTextContent(
      `/files/${FIXTURE_ROOT_ID}/view/${ENTRY_ID}`,
    );
    const stateText = screen.getByLabelText("History state").textContent ?? "";
    expect(JSON.parse(stateText)).toEqual({
      aegisMediaViewer: {
        v: 1,
        entryId: ENTRY_ID,
        context: OPENER_CONTEXT,
      },
    });
    expect(stateText).not.toContain("Holiday");
  });

  it("passes the opener's viewer context through history state", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID));
    await renderPanel(ENTRY_ID, {
      viewerContext: {source: "directory", rootId: FIXTURE_ROOT_ID, parentId: null, returnKey: "opener1"},
    });
    fireEvent.click(await screen.findByRole("link", {name: "Open photo"}));
    const stateText = screen.getByLabelText("History state").textContent ?? "";
    expect(JSON.parse(stateText).aegisMediaViewer.context).toEqual({
      source: "directory", rootId: FIXTURE_ROOT_ID, parentId: null, returnKey: "opener1",
    });
  });

  it("hides Open and Download without PREVIEW or EXPORT and says why", async () => {
    serveMedia(
      mediaEntry({downloadUrl: null}),
      mediaDetailsBody(ENTRY_ID, {permissions: {preview: false, export: false}}),
    );
    await renderPanel();
    await screen.findByText("Dimensions", {selector: "dt"});
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("Your access to this file does not include viewing or downloading it.")).toBeVisible();
  });

  it("names Open by kind for videos", async () => {
    serveMedia(
      mediaEntry({typeHint: "mp4", media: entryMediaFixture(ENTRY_ID, {kind: "video", durationMs: 1000})}),
      mediaDetailsBody(ENTRY_ID, {kind: "video"}),
    );
    await renderPanel();
    expect(await screen.findByRole("link", {name: "Open video"})).toBeVisible();
  });

  it("offers no Open without the opener's viewer context", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID));
    await renderPanel(ENTRY_ID, {viewerContext: undefined});
    await screen.findByText("Dimensions", {selector: "dt"});
    expect(screen.getByRole("link", {name: "Download original"})).toBeVisible();
    expect(screen.queryByRole("link", {name: /^Open/})).not.toBeInTheDocument();
  });

  it("hides Open for an asset the viewer cannot show", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID, {
      derivatives: {thumbnail: {state: "unsupported", url: null}, preview: {state: "unsupported", url: null}},
      original: {inlineUrl: null},
    }));
    await renderPanel();
    expect(await screen.findByText("This photo cannot be shown in the viewer.")).toBeVisible();
    expect(screen.queryByRole("link", {name: /^Open/})).not.toBeInTheDocument();
    expect(screen.getByRole("link", {name: "Download original"})).toBeVisible();
  });

  it("hides Open when the original is missing and no preview is ready", async () => {
    serveMedia(mediaEntry({sourceState: "missing", downloadUrl: null}), mediaDetailsBody(ENTRY_ID));
    await renderPanel();
    expect(await screen.findByText("The original is not available right now, so this photo cannot be opened."))
      .toBeVisible();
    expect(screen.queryByRole("link", {name: /^Open/})).not.toBeInTheDocument();
  });

  it("still opens a missing original whose preview is ready", async () => {
    serveMedia(mediaEntry({sourceState: "missing", downloadUrl: null}), mediaDetailsBody(ENTRY_ID, {
      derivatives: {
        thumbnail: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "thumbnail")},
        preview: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "preview")},
      },
    }));
    await renderPanel();
    expect(await screen.findByRole("link", {name: "Open photo"})).toBeVisible();
  });

  it("drops the unavailable-actions text for media entries only", async () => {
    serveMedia(mediaEntry(), mediaDetailsBody(ENTRY_ID));
    await renderPanel();
    await screen.findByRole("region", {name: "Photo details"});
    expect(screen.getByText("Originals remain read only.")).toBeVisible();
    expect(screen.queryByText(/not available in this version/i)).not.toBeInTheDocument();
  });

  it("requests no media details for non-media entries", async () => {
    let mediaRequests = 0;
    server.use(
      http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(details())),
      http.get(`/api/v1/entries/${ENTRY_ID}/media`, () => {
        mediaRequests += 1;
        return HttpResponse.json(mediaDetailsBody(ENTRY_ID));
      }),
    );
    await renderPanel();
    await screen.findByRole("heading", {name: "Holiday album.jpg"});
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mediaRequests).toBe(0);
    expect(screen.queryByRole("region", {name: /Photo details/})).not.toBeInTheDocument();
  });

  it("explains media details that are not available without offering a retry", async () => {
    serveMedia(
      mediaEntry(),
      () => HttpResponse.json({type: "catalog_not_found", title: "Catalog item not found"}, {status: 404}),
    );
    await renderPanel();
    expect(await screen.findByText("Media details are not available for this file.")).toBeVisible();
    expect(screen.queryByRole("button", {name: "Retry media details"})).not.toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "Holiday album.jpg"})).toBeVisible();
  });

  it("offers a retry after a failed media details request", async () => {
    let calls = 0;
    server.use(
      http.get(`/api/v1/entries/${ENTRY_ID}`, () => HttpResponse.json(mediaEntry())),
      http.get(`/api/v1/entries/${ENTRY_ID}/media`, () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({type: "media_unavailable", title: "Media unavailable"}, {status: 503})
          : HttpResponse.json(mediaDetailsBody(ENTRY_ID));
      }),
    );
    await renderPanel();
    expect(await screen.findByText("Media details could not be loaded.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", {name: "Retry media details"}));
    expect(await screen.findByText("Dimensions", {selector: "dt"})).toBeVisible();
  });

  it("closes private access and clears media details on a media 401", async () => {
    serveMedia(
      mediaEntry(),
      () => HttpResponse.json({type: "authentication_required", title: "Authentication required"}, {status: 401}),
    );
    const {queryClient} = await renderPanel();
    await waitFor(() => expect(isSessionAccessOpen()).toBe(false));
    // A still-mounted observer may rebuild an empty, refused query; no media data survives.
    expect(queryClient.getQueryCache().findAll({queryKey: ["media"]})
      .filter((query) => query.state.data !== undefined)).toHaveLength(0);
    expect(screen.queryByText("Dimensions", {selector: "dt"})).not.toBeInTheDocument();
  });
});
