import {invalidResponse, objectWithKeys, timestamp, uuid} from "../../api/validate";
import type {
  CapturedAtSource,
  DerivativeKind,
  DerivativeState,
  EntryMedia,
  MediaDerivative,
  MediaDetails,
  MediaKind,
  MediaMetadata,
  MediaOriginal,
  MediaRequestResult,
  RequestableDerivativeKind,
  SortDateSource,
  ThumbnailState,
} from "./types";

const MAX_URL_LENGTH = 2048;
const MAX_TEXT_LENGTH = 256;
const MAX_DIMENSION = 2_147_483_647;
// An opaque 32-hex HMAC token of the source fingerprint (never the raw fingerprint).
const FINGERPRINT = /^[0-9a-f]{32}$/;
const PRINTABLE_URL = /^[\x21-\x7e]+$/;
const PRINTABLE_TEXT = /^[\x20-\x7e]+$/;
const MAX_METADATA_TEXT_CODE_POINTS = 64;
// Controls, format characters (bidi overrides and isolates, zero-width characters),
// lone surrogates, private use, and line or paragraph separators.
const UNSAFE_TEXT = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/u;
const EDGE_WHITESPACE = /^\s|\s$/u;
// The backend's patterns for ffprobe codec names and container format names.
const CODEC = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$/;
const CONTAINER = /^[A-Za-z0-9][A-Za-z0-9._,-]{0,63}$/;
const CONTENT_TYPE = /^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/i;
const mediaKinds = new Set<MediaKind>(["photo", "video"]);
const thumbnailStates = new Set<ThumbnailState>(["pending", "ready", "failed", "unsupported", "quarantined"]);
const sortDateSources = new Set<SortDateSource>(["captured", "modified"]);
const capturedAtSources = new Set<CapturedAtSource>(["exif", "container"]);
const derivativeKinds = new Set<DerivativeKind>(["thumbnail", "preview", "poster", "transcode"]);
const derivativeStates = new Set<DerivativeState>([
  "missing", "pending", "ready", "failed", "unsupported", "quarantined",
]);

function member<T extends string>(value: unknown, choices: ReadonlySet<T>): T {
  if (typeof value !== "string" || !choices.has(value as T)) invalidResponse();
  return value as T;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") invalidResponse();
  return value;
}

/** Bounded printable ASCII for a structural string; an empty string reads as null. */
function nullablePrintable(value: unknown, maximum = MAX_TEXT_LENGTH): string | null {
  if (value === null || value === "") return null;
  if (typeof value !== "string" || value.length > maximum || !PRINTABLE_TEXT.test(value)) {
    invalidResponse();
  }
  return value;
}

/*
 * Optional display metadata degrades per field: one malformed value reads as
 * null (shown as unknown) instead of failing the whole body, which would also
 * withhold the delivery URLs. Structural fields (IDs, kinds, states, URLs,
 * permissions, keys) stay strict.
 */

function optionalInteger(value: unknown, minimum: number, maximum: number): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum
    ? value
    : null;
}

function optionalPattern(value: unknown, pattern: RegExp): string | null {
  return typeof value === "string" && pattern.test(value) ? value : null;
}

/** Trimmed Unicode text of 1–64 code points without controls, bidi or format characters. */
function optionalDisplayText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const codePoints = [...value].length;
  if (codePoints < 1 || codePoints > MAX_METADATA_TEXT_CODE_POINTS ||
      UNSAFE_TEXT.test(value) || EDGE_WHITESPACE.test(value)) return null;
  return value;
}

function optionalTimestamp(value: unknown): string | null {
  try {
    return timestamp(value);
  } catch {
    return null;
  }
}

function optionalMember<T extends string>(value: unknown, choices: ReadonlySet<T>): T | null {
  return typeof value === "string" && choices.has(value as T) ? value as T : null;
}

/**
 * Required query parameters of a media URL: a string pins the exact value,
 * null requires a non-empty value (the namespace and epoch are the server's).
 */
type UrlParameters = Readonly<Record<string, string | null>>;

function derivativeParameters(fingerprint: string | null): UrlParameters {
  return {f: fingerprint, n: null, e: null};
}

function originalParameters(disposition: "inline" | "attachment"): UrlParameters {
  return {d: disposition, n: null, e: null};
}

/**
 * A canonical same-origin media path. The exact pathname is fixed by the
 * caller, so a body can never point an `<img>`, `<video>` or download link at
 * another entry, another derivative or another origin. The contract's query
 * parameters must be present.
 */
function mediaUrl(
  value: unknown,
  expectedPath: string,
  parameters: UrlParameters,
): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || value.length > MAX_URL_LENGTH || !PRINTABLE_URL.test(value) ||
      !value.startsWith("/media/v1/") || value.includes("\\") || value.includes("#")) {
    invalidResponse();
  }
  let resolved: URL;
  try {
    resolved = new URL(value, window.location.origin);
  } catch {
    invalidResponse();
  }
  if (resolved.origin !== window.location.origin || resolved.pathname !== expectedPath ||
      `${resolved.pathname}${resolved.search}` !== value) {
    invalidResponse();
  }
  for (const [name, expected] of Object.entries(parameters)) {
    const actual = resolved.searchParams.get(name);
    if (actual === null || actual === "" || (expected !== null && actual !== expected)) invalidResponse();
  }
  return value;
}

export function derivativePath(entryId: string, kind: DerivativeKind): string {
  return `/media/v1/derivatives/${entryId}/${kind}`;
}

export function originalPath(entryId: string): string {
  return `/media/v1/originals/${entryId}`;
}

/**
 * An original's attachment URL (Download) for the entry `entryId`, or null: the
 * canonical same-origin path with `d=attachment` and the server's namespace and epoch.
 * Entry details and media details carry the same URL.
 */
export function validatedDownloadUrl(value: unknown, entryId: string): string | null {
  return mediaUrl(value, originalPath(entryId), originalParameters("attachment"));
}

const ENTRY_MEDIA_KEYS = [
  "durationMs", "height", "kind", "sortDate", "sortDateSource", "thumbnail", "thumbnailUrl", "width",
] as const;

/** Validate an entry summary's non-null `media` object for the entry `entryId`. */
export function validatedEntryMedia(value: unknown, entryId: string): EntryMedia {
  const fields = objectWithKeys(value, ENTRY_MEDIA_KEYS);
  const thumbnail = member(fields.thumbnail, thumbnailStates);
  const thumbnailUrl = mediaUrl(fields.thumbnailUrl, derivativePath(entryId, "thumbnail"), derivativeParameters(null));
  if (thumbnailUrl !== null && thumbnail !== "ready") invalidResponse();
  const sortDate = timestamp(fields.sortDate);
  if (sortDate === null) invalidResponse();
  return {
    kind: member(fields.kind, mediaKinds),
    thumbnail,
    sortDate,
    sortDateSource: member(fields.sortDateSource, sortDateSources),
    width: optionalInteger(fields.width, 1, MAX_DIMENSION),
    height: optionalInteger(fields.height, 1, MAX_DIMENSION),
    durationMs: optionalInteger(fields.durationMs, 0, Number.MAX_SAFE_INTEGER),
    thumbnailUrl,
  };
}

const METADATA_KEYS = [
  "audioCodec", "cameraMake", "cameraModel", "capturedAt", "capturedAtSource", "container",
  "durationMs", "hasLocation", "height", "orientation", "videoCodec", "width",
] as const;

function validatedMetadata(value: unknown): MediaMetadata {
  const fields = objectWithKeys(value, METADATA_KEYS);
  const capturedAt = optionalTimestamp(fields.capturedAt);
  return {
    width: optionalInteger(fields.width, 1, MAX_DIMENSION),
    height: optionalInteger(fields.height, 1, MAX_DIMENSION),
    orientation: optionalInteger(fields.orientation, 1, 8),
    capturedAt,
    // A source without a capture time describes nothing.
    capturedAtSource: capturedAt === null ? null : optionalMember(fields.capturedAtSource, capturedAtSources),
    cameraMake: optionalDisplayText(fields.cameraMake),
    cameraModel: optionalDisplayText(fields.cameraModel),
    durationMs: optionalInteger(fields.durationMs, 0, Number.MAX_SAFE_INTEGER),
    videoCodec: optionalPattern(fields.videoCodec, CODEC),
    audioCodec: optionalPattern(fields.audioCodec, CODEC),
    container: optionalPattern(fields.container, CONTAINER),
    hasLocation: boolean(fields.hasLocation),
  };
}

function validatedDerivatives(
  value: unknown,
  entryId: string,
  fingerprint: string,
  preview: boolean,
): Partial<Record<DerivativeKind, MediaDerivative>> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidResponse();
  const fields = value as Record<string, unknown>;
  const result: Partial<Record<DerivativeKind, MediaDerivative>> = {};
  for (const key of Object.keys(fields)) {
    const kind = member(key, derivativeKinds);
    const item = objectWithKeys(fields[key], ["state", "url"]);
    const state = member(item.state, derivativeStates);
    const url = mediaUrl(item.url, derivativePath(entryId, kind), derivativeParameters(fingerprint));
    if (url !== null && (state !== "ready" || !preview)) invalidResponse();
    result[kind] = {state, url};
  }
  return result;
}

function validatedOriginal(
  value: unknown,
  entryId: string,
  permissions: MediaDetails["permissions"],
): MediaOriginal {
  const fields = objectWithKeys(value, ["contentType", "downloadUrl", "inlineUrl", "playbackType"]);
  if (typeof fields.contentType !== "string" || !CONTENT_TYPE.test(fields.contentType)) invalidResponse();
  const inlineUrl = mediaUrl(fields.inlineUrl, originalPath(entryId), originalParameters("inline"));
  const downloadUrl = validatedDownloadUrl(fields.downloadUrl, entryId);
  if ((inlineUrl !== null && !permissions.preview) || (downloadUrl !== null && !permissions.export)) {
    invalidResponse();
  }
  return {
    contentType: fields.contentType,
    inlineUrl,
    downloadUrl,
    playbackType: nullablePrintable(fields.playbackType),
  };
}

const DETAILS_KEYS = [
  "derivatives", "entryId", "fingerprint", "kind", "metadata", "original", "permissions",
] as const;

/** Validate `GET /api/v1/entries/{id}/media` for the requested entry. */
export function validatedMediaDetails(value: unknown, entryId: string): MediaDetails {
  const fields = objectWithKeys(value, DETAILS_KEYS);
  if (uuid(fields.entryId) !== entryId) invalidResponse();
  if (typeof fields.fingerprint !== "string" || !FINGERPRINT.test(fields.fingerprint)) invalidResponse();
  const permissionFields = objectWithKeys(fields.permissions, ["export", "preview"]);
  const permissions = {
    preview: boolean(permissionFields.preview),
    export: boolean(permissionFields.export),
  };
  return {
    entryId,
    kind: member(fields.kind, mediaKinds),
    fingerprint: fields.fingerprint,
    metadata: validatedMetadata(fields.metadata),
    derivatives: validatedDerivatives(fields.derivatives, entryId, fields.fingerprint, permissions.preview),
    original: validatedOriginal(fields.original, entryId, permissions),
    permissions,
  };
}

const requestStates = new Set<MediaRequestResult["state"]>(["pending", "ready"]);

/** Validate a 202 interactive request answer for the requested derivative kind. */
export function validatedMediaRequestResult(
  value: unknown,
  kind: RequestableDerivativeKind,
): MediaRequestResult {
  const fields = objectWithKeys(value, ["kind", "state"]);
  if (fields.kind !== kind) invalidResponse();
  return {state: member(fields.state, requestStates), kind};
}
