import type {MediaMetadata} from "./types";

const pixels = new Intl.NumberFormat("en-US", {useGrouping: true});

/**
 * Displayed width and height. The server reports display dimensions with EXIF
 * orientation already applied, so they are never swapped here.
 */
export function displayDimensions(
  metadata: Pick<MediaMetadata, "width" | "height">,
): {width: number; height: number} | null {
  if (metadata.width === null || metadata.height === null) return null;
  return {width: metadata.width, height: metadata.height};
}

export function formatDimensions(dimensions: {width: number; height: number}): string {
  return `${pixels.format(dimensions.width)} × ${pixels.format(dimensions.height)} pixels`;
}

/** `m:ss` or `h:mm:ss`, rounded down to whole seconds. */
export function formatDuration(durationMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(durationMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
}

/** Camera make and model without repeating a make that the model already names. */
export function cameraText(metadata: Pick<MediaMetadata, "cameraMake" | "cameraModel">): string | null {
  const make = metadata.cameraMake?.trim() || null;
  const model = metadata.cameraModel?.trim() || null;
  if (make && model) {
    return model.toLowerCase().startsWith(make.toLowerCase()) ? model : `${make} ${model}`;
  }
  return model ?? make;
}

const RECORDED_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/;

/**
 * The wall-clock time a timestamp records, in its own offset rather than the
 * viewer's zone, such as "Dec 24, 2026, 6:30:00 PM (UTC+09:00)".
 */
export function formatRecordedTime(value: string): string | null {
  const match = RECORDED_TIME.exec(value);
  if (!match) return null;
  const [, year, month, day, hour, minute, second, zone] = match;
  const wallClock = new Date(0);
  wallClock.setUTCFullYear(Number(year), Number(month) - 1, Number(day));
  wallClock.setUTCHours(Number(hour), Number(minute), Number(second), 0);
  if (Number.isNaN(wallClock.getTime())) return null;
  const text = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(wallClock);
  const offset = zone === "Z" || zone === "+00:00" || zone === "-00:00" ? "UTC" : `UTC${zone}`;
  return `${text} (${offset})`;
}

const WALL_CLOCK = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d{1,9})?(?:Z|[+-]00:00)$/;

/**
 * Whether a timestamp is a wall-clock time: the media contract sends capture
 * and sort dates as the camera's (or file's) local clock reading, encoded with
 * `Z`. Such a value has no time zone, so it is shown as is, never converted.
 */
export function isWallClockTime(value: string): boolean {
  return WALL_CLOCK.test(value);
}

/**
 * A capture time as the camera recorded it. Wall-clock values (`Z`) are
 * formatted in UTC with no zone suffix, so the digits are the camera's own;
 * a value with a real offset keeps that offset, as `formatRecordedTime` does.
 */
export function formatCameraTime(value: string): string | null {
  if (!isWallClockTime(value)) return formatRecordedTime(value);
  const recorded = formatRecordedTime(value);
  return recorded === null ? null : recorded.replace(/ \(UTC\)$/, "");
}

/** The wall-clock digits of a `Z`-encoded time, without the zone designator or fraction. */
export function wallClockDigits(value: string): string | null {
  return WALL_CLOCK.exec(value)?.[1] ?? null;
}
