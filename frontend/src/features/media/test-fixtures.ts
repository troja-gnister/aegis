import type {EntryMedia, MediaKind} from "./types";

/** Synthetic media fixtures for tests; no real names, hosts or camera serials. */
export const FIXTURE_FINGERPRINT = "a".repeat(64);
export const FIXTURE_NAMESPACE_PARAMETER = "fixture-namespace";

export function thumbnailUrlFor(entryId: string, epoch = 4): string {
  return `/media/v1/derivatives/${entryId}/thumbnail?f=${FIXTURE_FINGERPRINT}&n=${FIXTURE_NAMESPACE_PARAMETER}&e=${epoch}`;
}

export function derivativeUrlFor(entryId: string, kind: string): string {
  return `/media/v1/derivatives/${entryId}/${kind}?f=${FIXTURE_FINGERPRINT}&n=${FIXTURE_NAMESPACE_PARAMETER}&e=4`;
}

export function originalUrlFor(entryId: string, disposition: "inline" | "attachment"): string {
  return `/media/v1/originals/${entryId}?d=${disposition}&n=${FIXTURE_NAMESPACE_PARAMETER}&e=4`;
}

/** An entry summary `media` object (ready photo thumbnail by default). */
export function entryMediaFixture(entryId: string, overrides: Partial<EntryMedia> = {}): EntryMedia {
  return {
    kind: "photo",
    thumbnail: "ready",
    sortDate: "2027-01-10T08:00:00Z",
    sortDateSource: "captured",
    width: 4032,
    height: 3024,
    durationMs: null,
    thumbnailUrl: thumbnailUrlFor(entryId),
    ...overrides,
  };
}

/**
 * A `GET /api/v1/entries/{id}/media` wire body (photo with PREVIEW and EXPORT
 * by default). `metadata` and `original` overrides merge into the defaults;
 * any other key replaces the default value, so tests can send invalid bodies.
 */
export function mediaDetailsBody(entryId: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const kind = (overrides.kind ?? "photo") as MediaKind;
  const permissions = (overrides.permissions ?? {preview: true, export: true}) as {preview: boolean; export: boolean};
  const {metadata, derivatives, original, ...rest} = overrides as {
    metadata?: Record<string, unknown>;
    derivatives?: Record<string, unknown>;
    original?: Record<string, unknown>;
  };
  const video = kind === "video";
  return {
    entryId,
    kind,
    fingerprint: FIXTURE_FINGERPRINT,
    metadata: {
      width: 4032,
      height: 3024,
      orientation: video ? null : 1,
      capturedAt: "2027-01-10T08:00:00+00:00",
      capturedAtSource: video ? "container" : "exif",
      cameraMake: video ? null : "Synthetic Optics",
      cameraModel: video ? null : "Synthetic Optics Model 7",
      durationMs: video ? 3_723_000 : null,
      videoCodec: video ? "h264" : null,
      audioCodec: video ? "aac" : null,
      container: video ? "mp4" : null,
      hasLocation: false,
      ...metadata,
    },
    derivatives: derivatives ?? (video
      ? {
        thumbnail: {state: "ready", url: permissions.preview ? derivativeUrlFor(entryId, "thumbnail") : null},
        poster: {state: "ready", url: permissions.preview ? derivativeUrlFor(entryId, "poster") : null},
        transcode: {state: "missing", url: null},
      }
      : {
        thumbnail: {state: "ready", url: permissions.preview ? derivativeUrlFor(entryId, "thumbnail") : null},
        preview: {state: "missing", url: null},
      }),
    original: {
      contentType: video ? "video/mp4" : "image/jpeg",
      inlineUrl: permissions.preview ? originalUrlFor(entryId, "inline") : null,
      downloadUrl: permissions.export ? originalUrlFor(entryId, "attachment") : null,
      playbackType: video ? "video/mp4; codecs=\"avc1.640028, mp4a.40.2\"" : null,
      ...original,
    },
    permissions,
    ...rest,
  };
}
