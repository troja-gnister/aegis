import {describe, expect, it} from "vitest";
import {parseMediaViewerState, viewerLocationState, viewerPath} from "./viewer-route";

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
    const timeline = viewerLocationState(ENTRY_ID, {source: "timeline", rootId: ROOT_ID, returnKey: "k9"});

    for (const state of [directory, rootDirectory, timeline]) {
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
      source: "timeline", rootId: ROOT_ID, returnKey: "k",
    }}}],
    ["a display name", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, displayName: "Private.jpg", context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k",
    }}}],
    ["retained filters", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "directory", rootId: ROOT_ID, parentId: null, returnKey: "k", filters: {v: 1, prefix: "private"},
    }}}],
    ["a malformed entry ID", {aegisMediaViewer: {v: 1, entryId: "../x", context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "k",
    }}}],
    ["an unknown source", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "album", rootId: ROOT_ID, returnKey: "k",
    }}}],
    ["a malformed route key", {aegisMediaViewer: {v: 1, entryId: ENTRY_ID, context: {
      source: "timeline", rootId: ROOT_ID, returnKey: "a key with spaces",
    }}}],
  ])("rejects %s", (_name, state) => {
    expect(parseMediaViewerState(state)).toBeNull();
  });

  it("refuses to build state with a malformed route key", () => {
    expect(viewerLocationState(ENTRY_ID, {source: "timeline", rootId: ROOT_ID, returnKey: ""})).toBeNull();
  });
});
