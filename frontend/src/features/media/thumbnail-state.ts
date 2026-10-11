import {registerPrivateStateCleanup} from "../auth/cache";
import {MEDIA_KIND_LABEL} from "./classify";
import type {EntryMedia, MediaKind} from "./types";

/**
 * What a thumbnail box shows. `unavailable` is a ready thumbnail the caller
 * may not preview; `unknown` is a media file the catalog has not described
 * yet; `load-failed` is a ready thumbnail whose image failed to load.
 */
export type ThumbnailDisplayState =
  | "ready" | "pending" | "failed" | "unsupported" | "unavailable" | "unknown" | "load-failed";

export function thumbnailDisplayState(media: EntryMedia | null): ThumbnailDisplayState {
  if (media === null) return "unknown";
  switch (media.thumbnail) {
    case "ready": return media.thumbnailUrl === null ? "unavailable" : "ready";
    case "pending": return "pending";
    case "unsupported": return "unsupported";
    case "failed":
    case "quarantined": return "failed";
  }
}

export function thumbnailStateLabel(kind: MediaKind, state: ThumbnailDisplayState): string {
  const label = MEDIA_KIND_LABEL[kind];
  switch (state) {
    case "ready": return `${label} thumbnail`;
    case "pending": return `${label}, thumbnail is being prepared`;
    case "failed": return `${label}, thumbnail could not be created`;
    case "load-failed": return `${label}, thumbnail could not be loaded`;
    case "unsupported": return `${label}, thumbnails are not supported for this format`;
    case "unavailable": return `${label}, thumbnail is not available to you`;
    case "unknown": return label;
  }
}

const MAX_FAILED_THUMBNAIL_URLS = 1024;
const failedThumbnailUrls = new Set<string>();
// Thumbnail URLs carry the cache namespace and root epoch; logout and account
// switches forget every remembered failure with the rest of the private state.
registerPrivateStateCleanup(() => failedThumbnailUrls.clear());

/** Whether this exact thumbnail URL already failed to load in this session. */
export function hasThumbnailFailed(url: string): boolean {
  return failedThumbnailUrls.has(url);
}

/**
 * Remember a failed thumbnail URL so remounted rows (virtualized lists) do
 * not request it again. Bounded; the oldest failures are forgotten first.
 */
export function rememberThumbnailFailure(url: string): void {
  failedThumbnailUrls.delete(url);
  failedThumbnailUrls.add(url);
  while (failedThumbnailUrls.size > MAX_FAILED_THUMBNAIL_URLS) {
    const oldest = failedThumbnailUrls.values().next().value as string | undefined;
    if (oldest === undefined) break;
    failedThumbnailUrls.delete(oldest);
  }
}
