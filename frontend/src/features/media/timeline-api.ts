import {apiRequest} from "../../api/http";
import {invalidResponse, objectWithKeys, uuid} from "../../api/validate";
import {assertAuthority, authorityBoundRequest} from "../auth/authority";
import {capturePrivateState} from "../auth/cache";
import {SUMMARY_KEYS, validatedSummary} from "../files/api";
import type {TimelineFilter, TimelineItem, TimelineKind, TimelinePage} from "./types";

/** Items per timeline page; the contract allows up to 120 and defaults to 60. */
export const TIMELINE_PAGE_LIMIT = 60;
const MAX_TIMELINE_ITEMS = 120;
const TIMELINE_RESPONSE_BYTES = 1024 * 1024;
const MAX_CURSOR_BYTES = 8192;
const timelineKinds: ReadonlySet<string> = new Set<TimelineKind>(["all", "photo", "video"]);

export type TimelineInput = TimelineFilter & {
  namespace: string;
  rootId: string;
  rootEpoch: number;
};

function cursor(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || value.length < 1 || value.length > MAX_CURSOR_BYTES ||
      !/^[\x20-\x7e]+$/.test(value) || value.startsWith(".")) invalidResponse();
  return value;
}

function validatedItem(value: unknown, rootId: string, kind: TimelineKind): TimelineItem {
  const fields = objectWithKeys(value, [...SUMMARY_KEYS, "parentId"]);
  const summaryFields = Object.fromEntries(SUMMARY_KEYS.map((key) => [key, fields[key]]));
  const summary = validatedSummary(summaryFields, rootId);
  // Timeline items are media files of the requested kind; anything else is a contract breach.
  if (summary.kind !== "file" || summary.media === null) invalidResponse();
  if (kind !== "all" && summary.media.kind !== kind) invalidResponse();
  return {
    ...summary,
    media: summary.media,
    parentId: fields.parentId === null ? null : uuid(fields.parentId),
  };
}

function validatedTimelinePage(value: unknown, rootId: string, kind: TimelineKind): TimelinePage {
  const fields = objectWithKeys(value, ["contractVersion", "items", "nextCursor"]);
  if (fields.contractVersion !== 1 || !Array.isArray(fields.items) || fields.items.length > MAX_TIMELINE_ITEMS) {
    invalidResponse();
  }
  const items = fields.items.map((item) => validatedItem(item, rootId, kind));
  if (new Set(items.map((item) => item.id)).size !== items.length) invalidResponse();
  return {items, nextCursor: cursor(fields.nextCursor), contractVersion: 1};
}

function assertInput(input: TimelineInput, pageCursor: string | null): void {
  if (!input || typeof input.namespace !== "string" || input.namespace.length < 1 || input.namespace.length > 128 ||
      !Number.isSafeInteger(input.rootEpoch) || input.rootEpoch < 0 || !timelineKinds.has(input.kind) ||
      (input.year !== null && (!Number.isInteger(input.year) || input.year < 1 || input.year > 9999))) {
    invalidResponse();
  }
  uuid(input.rootId);
  if (pageCursor !== null) cursor(pageCursor);
}

/**
 * One keyset page of a root's media timeline, newest first. The request and
 * its result belong to the session and namespace that started it.
 */
export async function fetchTimeline(
  input: TimelineInput,
  pageCursor: string | null,
  signal: AbortSignal,
): Promise<TimelinePage> {
  assertInput(input, pageCursor);
  const snapshot = capturePrivateState();
  assertAuthority(snapshot, signal, input.namespace);
  const parameters = new URLSearchParams({kind: input.kind, limit: String(TIMELINE_PAGE_LIMIT)});
  if (input.year !== null) parameters.set("year", String(input.year).padStart(4, "0"));
  if (pageCursor !== null) parameters.set("cursor", pageCursor);
  const response = await authorityBoundRequest(
    apiRequest<unknown>(`/api/v1/roots/${input.rootId}/timeline?${parameters.toString()}`, {
      signal, maxResponseBytes: TIMELINE_RESPONSE_BYTES,
    }),
    snapshot,
    signal,
    input.namespace,
  );
  const result = validatedTimelinePage(response, input.rootId, input.kind);
  assertAuthority(snapshot, signal, input.namespace);
  return result;
}
