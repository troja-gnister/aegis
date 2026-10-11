import type {MediaDetails} from "../types";

export type VideoSourceChoice =
  | {source: "original"; url: string}
  | {source: "transcode"}
  | {source: "none"};

/** `HTMLMediaElement.canPlayType`: "", "maybe" or "probably". */
export type CanPlayType = (type: string) => string;

/** Ask a detached video element whether this browser can play a type. */
export function browserCanPlayType(type: string): string {
  try {
    return document.createElement("video").canPlayType(type);
  } catch {
    return "";
  }
}

/**
 * Which source plays a video: the inline original when the server probed a
 * codec string and the browser does not answer "" for it, otherwise the
 * on-demand transcode. Without PREVIEW nothing plays.
 */
export function chooseVideoSource(details: MediaDetails, canPlayType: CanPlayType = browserCanPlayType): VideoSourceChoice {
  if (!details.permissions.preview) return {source: "none"};
  const {inlineUrl, playbackType} = details.original;
  if (inlineUrl !== null && playbackType !== null && canPlayType(playbackType) !== "") {
    return {source: "original", url: inlineUrl};
  }
  return {source: "transcode"};
}
