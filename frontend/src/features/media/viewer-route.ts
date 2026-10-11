import {UUID} from "../../api/validate";
import type {SourceState} from "../files/types";
import type {
  DerivativeState,
  MediaDetails,
  MediaViewerContext,
  MediaViewerState,
  TimelineFilter,
  TimelineKind,
} from "./types";

const ROUTE_KEY = /^[A-Za-z0-9_-]{1,64}$/;
const YEAR = /^[0-9]{4}$/;
const timelineKinds: ReadonlySet<string> = new Set<TimelineKind>(["all", "photo", "video"]);

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

function isTimelineKind(value: unknown): value is TimelineKind {
  return typeof value === "string" && timelineKinds.has(value);
}

function isTimelineYear(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 9999;
}

/** The viewer route for one entry of a root. */
export function viewerPath(rootId: string, entryId: string): string {
  if (!isUuid(rootId) || !isUuid(entryId)) throw new Error("Viewer path needs a root and entry ID");
  return `/files/${rootId}/view/${entryId}`;
}

/** The Files route of a root. */
export function filesPath(rootId: string): string {
  if (!isUuid(rootId)) throw new Error("Files path needs a root ID");
  return `/files/${rootId}`;
}

/**
 * The Photos timeline route of a root. The kind filter and year live only in
 * the URL's search parameters; "all" and no year are the defaults and omitted.
 */
export function photosPath(rootId: string, filter: TimelineFilter = {kind: "all", year: null}): string {
  if (!isUuid(rootId)) throw new Error("Photos path needs a root ID");
  const search = timelineSearchParameters(filter).toString();
  return `/files/${rootId}/photos${search ? `?${search}` : ""}`;
}

/** Search parameters for a timeline filter (defaults omitted). */
export function timelineSearchParameters(filter: TimelineFilter): URLSearchParams {
  const parameters = new URLSearchParams();
  if (filter.kind !== "all") parameters.set("kind", filter.kind);
  if (filter.year !== null) parameters.set("year", String(filter.year).padStart(4, "0"));
  return parameters;
}

/**
 * The timeline filter from route search parameters. Anything unknown or
 * malformed reads as the default, so a hand-edited URL never reaches the API.
 */
export function parseTimelineFilter(parameters: URLSearchParams): TimelineFilter {
  const kindValue = parameters.get("kind");
  const yearValue = parameters.get("year");
  const kind = isTimelineKind(kindValue) ? kindValue : "all";
  const year = yearValue !== null && YEAR.test(yearValue) && isTimelineYear(Number(yearValue))
    ? Number(yearValue)
    : null;
  return {kind, year};
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
  if (fields.source === "timeline" && keys === "kind,returnKey,rootId,source,year" &&
      isTimelineKind(fields.kind) && (fields.year === null || isTimelineYear(fields.year))) {
    return {
      source: "timeline", rootId: fields.rootId, returnKey: fields.returnKey, kind: fields.kind, year: fields.year,
    };
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
