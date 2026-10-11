import {useEffect} from "react";
import type {MediaDetails} from "../types";

/** Only canonical same-origin media paths are ever prefetched. */
export function isSameOriginMediaUrl(url: string): boolean {
  if (!url.startsWith("/media/v1/") || url.startsWith("//")) return false;
  try {
    const resolved = new URL(url, window.location.origin);
    return resolved.origin === window.location.origin && resolved.pathname.startsWith("/media/v1/");
  } catch {
    return false;
  }
}

/** The image a neighbor would show first: a photo's ready preview, or a video's poster. */
export function neighborImageUrl(details: MediaDetails | undefined): string | null {
  if (!details || !details.permissions.preview) return null;
  const derivative = details.derivatives[details.kind === "photo" ? "preview" : "poster"];
  return derivative?.state === "ready" ? derivative.url : null;
}

/**
 * Warm the browser's image cache for the previous and next items with
 * detached `Image` objects on same-origin URLs. When the neighbors change
 * (rapid swiping) or the viewer closes, unfinished downloads are abandoned by
 * clearing their source; a missing preview is never requested for a neighbor.
 */
export function useNeighborImagePrefetch(urls: readonly (string | null)[]): void {
  const key = urls.filter((url): url is string => url !== null && isSameOriginMediaUrl(url)).join("\n");
  useEffect(() => {
    if (key === "") return;
    const images = key.split("\n").map((url) => {
      const image = new Image();
      image.decoding = "async";
      image.src = url;
      return image;
    });
    return () => {
      for (const image of images) image.src = "";
    };
  }, [key]);
}
