/** Media contract types shared by the file browser, the Photos timeline and the viewer. */

export type MediaKind = "photo" | "video";

/** Thumbnail state carried by every entry summary's `media` field. */
export type ThumbnailState = "pending" | "ready" | "failed" | "unsupported" | "quarantined";

export type SortDateSource = "captured" | "modified";

/** The compact `media` object on entry summaries; `null` on the summary for non-media entries. */
export type EntryMedia = {
  kind: MediaKind;
  thumbnail: ThumbnailState;
  /** RFC 3339; a capture time only when `sortDateSource` is `"captured"`. */
  sortDate: string;
  sortDateSource: SortDateSource;
  /** Display dimensions, with EXIF orientation already applied by the server. */
  width: number | null;
  height: number | null;
  durationMs: number | null;
  /** Present only when the thumbnail is ready and the caller has PREVIEW. Same-origin path. */
  thumbnailUrl: string | null;
};

export type DerivativeKind = "thumbnail" | "preview" | "poster" | "transcode";
export type DerivativeState = "missing" | "pending" | "ready" | "failed" | "unsupported" | "quarantined";

export type MediaDerivative = {
  state: DerivativeState;
  /** Non-null only when the state is ready and the permission allows it. Same-origin path. */
  url: string | null;
};

export type CapturedAtSource = "exif" | "container";

export type MediaMetadata = {
  /** Display width and height, with EXIF orientation already applied by the server. */
  width: number | null;
  height: number | null;
  /** EXIF orientation 1–8, or null. */
  orientation: number | null;
  /** Offset-qualified RFC 3339 capture time; never a filesystem time. */
  capturedAt: string | null;
  capturedAtSource: CapturedAtSource | null;
  cameraMake: string | null;
  cameraModel: string | null;
  durationMs: number | null;
  videoCodec: string | null;
  audioCodec: string | null;
  container: string | null;
  hasLocation: boolean;
};

export type MediaOriginal = {
  contentType: string;
  /** `/media/v1/originals/<entry>?d=inline…`, present only for PREVIEW and browser-native types. */
  inlineUrl: string | null;
  /** `/media/v1/originals/<entry>?d=attachment…`, present only with EXPORT. */
  downloadUrl: string | null;
  /** A `canPlayType` argument for videos, such as `video/mp4; codecs="avc1.640028, mp4a.40.2"`. */
  playbackType: string | null;
};

export type MediaPermissions = {
  preview: boolean;
  export: boolean;
};

/** `GET /api/v1/entries/{id}/media`. A derivative kind absent from the map is treated as missing. */
export type MediaDetails = {
  entryId: string;
  kind: MediaKind;
  fingerprint: string;
  metadata: MediaMetadata;
  derivatives: Partial<Record<DerivativeKind, MediaDerivative>>;
  original: MediaOriginal;
  permissions: MediaPermissions;
};

/** Derivatives a viewer may request interactively. */
export type RequestableDerivativeKind = "preview" | "transcode";

/** `POST /api/v1/entries/{id}/media/requests` (202). */
export type MediaRequestResult = {
  state: "pending" | "ready";
  kind: RequestableDerivativeKind;
};

/**
 * Where the viewer was opened from. History state holds only opaque IDs that
 * already appear in URLs; names, filters and cursors stay in in-memory,
 * namespace-owned stores keyed by `returnKey` (the opener's location key).
 */
export type MediaViewerContext =
  | {source: "directory"; rootId: string; parentId: string | null; returnKey: string}
  | {source: "timeline"; rootId: string; returnKey: string};

export type MediaViewerState = {
  aegisMediaViewer: {
    v: 1;
    entryId: string;
    context: MediaViewerContext;
  };
};
