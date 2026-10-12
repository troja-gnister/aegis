import {describe, expect, it} from "vitest";
import {
  filesPath,
  parseMediaViewerState,
  parseTimelineFilter,
  photosPath,
  viewerLocationState,
  viewerPath,
} from "./viewer-route";

const ROOT_ID = "11111111-1111-4111-8111-111111111111";
const PARENT_ID = "22222222-2222-4222-8222-222222222222";
const ENTRY_ID = "33333333-3333-4333-8333-333333333333";

describe("viewer route state", () => {
  it("builds the per-root viewer path from validated IDs only", () => {
    expect(viewerPath(ROOT_ID, ENTRY_ID)).toBe(`/files/${ROOT_ID}/view/${ENTRY_ID}`);
    expect(() => viewerPath("../roots", ENTRY_ID)).toThrow();
    expect(() => viewerPath(ROOT_ID, "x")).toThrow();
  });

  it("round-trips directory and timeline contexts", () => {
    const directory = viewerLocationState(ENTRY_ID, {
      source: "directory", rootId: ROOT_ID, parentId: PARENT_ID, returnKey: "abc123",
    });
    const rootDirectory = viewerLocationState(ENTRY_ID, {
      source: "directory", rootId: ROOT_ID, parentId: null, returnKey: "default",
    });
    const timeline = viewerLocationState(ENTRY_ID, {
      source: "timeline", rootId: ROOT_ID, returnKey: "k9", kind: "all", year: null,
    });
    const filteredTimeline = viewerLocationState(ENTRY_ID, {
      source: "timeline", rootId: ROOT_ID, returnKey: "k10", kind: "video", year: 2026,
    });
    const search = viewerLocationState(ENTRY_ID, {
      source: "search", rootId: ROOT_ID, parentId: PARENT_ID, returnKey: "k11",
    });

    expect(search?.aegisMediaViewer.context).toEqual({
      source: "search", rootId: ROOT_ID, parentId: PARENT_ID, returnKey: "k11",
    });
    for (const state of [directory, rootDirectory, timeline, filteredTimeline, search]) {
      expect(state).not.toBeNull();
      expect(parseMediaViewerState(JSON.parse(JSON.stringify(state)))).toEqual(state);
    }
    expect(directory?.aegisMediaViewer).toEqual({
      v: 1,
      entryId: ENTRY_ID,
      context: {source: "directory", rootId: ROOT_ID, parentId: PARENT_ID, returnKey: "abc123"},
    });
  });

  it.each([
    ["no state", null],
    ["another app's state", {usr: {}}],
    ["a newer version", {aegisMediaViewer: {v: 2, entryId: ENTRY_ID, context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k", kind: "all", year: null,
    }}}],
    ["a display name", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, displayName: "Private.jpg", context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k", kind: "all", year: null,
    }}}],
    ["retained filters", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "directory", rootId: ROOT_ID, parentId: null, returnKey: "k", filters: {v: 1, prefix: "private"},
    }}}],
    ["search filters", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "search", rootId: ROOT_ID, parentId: null, returnKey: "k", filters: {v: 1, type: ["jpg"]},
    }}}],
    ["a search without its route folder", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "search", rootId: ROOT_ID, returnKey: "k",
    }}}],
    ["a malformed entry ID", {aegisMediaViewer: {v: 1, entryId: "../x", context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k", kind: "all", year: null,
    }}}],
    ["an unknown source", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "album", rootId: ROOT_ID, returnKey: "k",
    }}}],
    ["a timeline context without its filter", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k",
    }}}],
    ["an unknown timeline kind", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k", kind: "audio", year: null,
    }}}],
    ["a textual timeline year", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k", kind: "all", year: "2026",
    }}}],
    ["a timeline context with a cursor", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k", kind: "all", year: null, cursor: "opaque",
    }}}],
    ["a malformed route key", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "a key with spaces", kind: "all", year: null,
    }}}],
  ])("rejects %s", (_name, state) => {
    expect(parseMediaViewerState(state)).toBeNull();
  });

  it("refuses to build state with a malformed route key", () => {
    expect(viewerLocationState(ENTRY_ID, {
      source: "timeline", rootId: ROOT_ID, returnKey: "", kind: "all", year: null,
    })).toBeNull();
  });
});

describe("Files and Photos routes", () => {
  it("builds per-root paths with the timeline filter in search parameters only", () => {
    expect(filesPath(ROOT_ID)).toBe(`/files/${ROOT_ID}`);
    expect(photosPath(ROOT_ID)).toBe(`/files/${ROOT_ID}/photos`);
    expect(photosPath(ROOT_ID, {kind: "video", year: null})).toBe(`/files/${ROOT_ID}/photos?kind=video`);
    expect(photosPath(ROOT_ID, {kind: "photo", year: 2026})).toBe(`/files/${ROOT_ID}/photos?kind=photo&year=2026`);
    expect(() => photosPath("../x")).toThrow();
  });

  it.each([
    ["", {kind: "all", year: null}],
    ["kind=photo", {kind: "photo", year: null}],
    ["kind=video&year=2026", {kind: "video", year: 2026}],
    ["kind=all", {kind: "all", year: null}],
    ["kind=audio", {kind: "all", year: null}],
    ["kind=PHOTO", {kind: "all", year: null}],
    ["year=26", {kind: "all", year: null}],
    ["year=0000", {kind: "all", year: null}],
    ["year=2026.5", {kind: "all", year: null}],
  ])("parses the timeline filter from %j", (search, expected) => {
    expect(parseTimelineFilter(new URLSearchParams(search))).toEqual(expected);
  });
});
