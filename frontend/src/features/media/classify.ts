import type {EntryMedia, MediaKind} from "./types";

/**
 * Extension allowlists from the media design (M4). `typeHint` on an entry is
 * its lowercase extension, never a MIME family such as "image".
 */
export const PHOTO_EXTENSIONS = [
  "jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "avif", "bmp", "tif", "tiff",
] as const;

export const VIDEO_EXTENSIONS = [
  "mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "mts", "m2ts",
] as const;

const photoExtensions: ReadonlySet<string> = new Set(PHOTO_EXTENSIONS);
const videoExtensions: ReadonlySet<string> = new Set(VIDEO_EXTENSIONS);

/** Classify a file extension (with or without a leading dot, any case) as a media kind. */
export function classifyExtension(extension: string | null | undefined): MediaKind | null {
  if (typeof extension !== "string") return null;
  const normalized = extension.startsWith(".") ? extension.slice(1).toLowerCase() : extension.toLowerCase();
  if (photoExtensions.has(normalized)) return "photo";
  if (videoExtensions.has(normalized)) return "video";
  return null;
}

export type ClassifiableEntry = {
  kind: string;
  typeHint: string | null;
  media: EntryMedia | null;
};

/**
 * The media kind of an entry: the server's `media.kind` when present, else the
 * extension classification of a regular file. Directories, links and special
 * entries are never media, whatever their names end with.
 */
export function mediaKindOf(entry: ClassifiableEntry): MediaKind | null {
  if (entry.media !== null) return entry.media.kind;
  return entry.kind === "file" ? classifyExtension(entry.typeHint) : null;
}

export const MEDIA_KIND_LABEL: Record<MediaKind, string> = {
  photo: "Photo",
  video: "Video",
};
