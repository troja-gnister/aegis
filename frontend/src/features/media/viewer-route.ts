import {UUID} from "../../api/validate";
import type {SourceState} from "../files/types";
import type {DerivativeState, MediaDetails, MediaViewerContext, MediaViewerState} from "./types";

const ROUTE_KEY = /^[A-Za-z0-9_-]{1,64}$/;

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

/** The viewer route for one entry of a root (added by the viewer task). */
export function viewerPath(rootId: string, entryId: string): string {
  if (!isUuid(rootId) || !isUuid(entryId)) throw new Error("Viewer path needs a root and entry ID");
  return `/files/${rootId}/view/${entryId}`;
}

/**
 * History state for opening the viewer, or null when an ID or the route key is
 * malformed. It carries opaque IDs and a route key only.
 */
export function viewerLocationState(entryId: string, context: MediaViewerContext): MediaViewerState | null {
  return parseMediaViewerState({aegisMediaViewer: {v: 1, entryId, context}});
}

function parseContext(value: unknown): MediaViewerContext | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const fields = value as Record<string, unknown>;
  const keys = Object.keys(fields).sort().join(",");
  if (!isUuid(fields.rootId) || typeof fields.returnKey !== "string" || !ROUTE_KEY.test(fields.returnKey)) {
    return null;
  }
  if (fields.source === "directory" && keys === "parentId,returnKey,rootId,source" &&
      (fields.parentId === null || isUuid(fields.parentId))) {
    return {source: "directory", rootId: fields.rootId, parentId: fields.parentId, returnKey: fields.returnKey};
  }
  if (fields.source === "timeline" && keys === "returnKey,rootId,source") {
    return {source: "timeline", rootId: fields.rootId, returnKey: fields.returnKey};
  }
  return null;
}

/** Strictly parse `location.state` for the viewer; anything else (including a reload's leftovers) is null. */
export function parseMediaViewerState(state: unknown): MediaViewerState | null {
  if (!state || typeof state !== "object" || Array.isArray(state)) return null;
  const viewer = (state as Record<string, unknown>).aegisMediaViewer;
  if (!viewer || typeof viewer !== "object" || Array.isArray(viewer)) return null;
  const fields = viewer as Record<string, unknown>;
  if (Object.keys(fields).sort().join(",") !== "context,entryId,v" || fields.v !== 1 || !isUuid(fields.entryId)) {
    return null;
  }
  const context = parseContext(fields.context);
  if (context === null) return null;
  return {aegisMediaViewer: {v: 1, entryId: fields.entryId, context}};
}

const BLOCKED_DERIVATIVE_STATES: ReadonlySet<DerivativeState> = new Set(["unsupported", "quarantined"]);

/**
 * Whether the viewer has a usable route to a picture or video: PREVIEW, and
 * either a ready viewing derivative (photo preview, video transcode), or a
 * readable source with an inline original or a derivative that can still be
 * requested. An unsupported or quarantined asset, or a source that is not
 * present, would only reach a 409 or 404 in the viewer.
 */
export function canOpenInViewer(details: MediaDetails, sourceState: SourceState): boolean {
  if (!details.permissions.preview) return false;
  const viewing = details.derivatives[details.kind === "photo" ? "preview" : "transcode"];
  if (viewing?.state === "ready") return true;
  if (sourceState !== "present") return false;
  if (details.original.inlineUrl !== null) return true;
  return viewing === undefined || !BLOCKED_DERIVATIVE_STATES.has(viewing.state);
}
