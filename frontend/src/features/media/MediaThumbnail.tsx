import {useState, type CSSProperties} from "react";
import {mediaKindOf, type ClassifiableEntry} from "./classify";
import {
  hasThumbnailFailed,
  rememberThumbnailFailure,
  thumbnailDisplayState,
  thumbnailStateLabel,
  type ThumbnailDisplayState,
} from "./thumbnail-state";
import type {MediaKind} from "./types";
import "./media.css";

export type MediaThumbnailProps = {
  entry: ClassifiableEntry;
  /** Box edge in CSS pixels, also used for the image's width and height attributes. */
  size?: number;
  className?: string;
  id?: string;
  /**
   * ID for a visually hidden text node holding the state label, for an
   * enclosing control's `aria-describedby` (a button's content is presentational).
   */
  descriptionId?: string;
};

const ICON: Record<MediaKind, string> = {photo: "▧", video: "▶"};
// Every non-ready state has a distinct glyph, so no state is told apart by colour alone.
const BADGE: Partial<Record<ThumbnailDisplayState, string>> = {
  pending: "…",
  failed: "!",
  "load-failed": "!",
  unsupported: "×",
  unavailable: "–",
  unknown: "?",
};

/**
 * A fixed-size thumbnail box for a media entry. It renders the lazy image only
 * for a ready thumbnail with a URL and otherwise a kind icon whose accessible
 * name states why there is no image. A failed image falls back to the icon,
 * and the URL is remembered for the session, so neither a re-render nor a
 * remounted virtual row requests it again. Non-media entries render nothing.
 */
export function MediaThumbnail({entry, size = 44, className, id, descriptionId}: MediaThumbnailProps) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const kind = mediaKindOf(entry);
  if (kind === null) return null;

  const url = entry.media?.thumbnail === "ready" ? entry.media.thumbnailUrl : null;
  let state = thumbnailDisplayState(entry.media);
  if (state === "ready" && url !== null && (failedUrl === url || hasThumbnailFailed(url))) state = "load-failed";
  const label = thumbnailStateLabel(kind, state);
  const style = {"--media-thumbnail-size": `${size}px`} as CSSProperties;
  const classes = ["media-thumbnail", `media-thumbnail--${kind}`, className].filter(Boolean).join(" ");
  const description = descriptionId === undefined
    ? null
    : <span id={descriptionId} className="media-thumbnail__description">{label}</span>;

  if (state === "ready" && url !== null) {
    return (
      <span id={id} className={classes} data-state="ready" style={style}>
        <img
          className="media-thumbnail__image"
          src={url}
          alt=""
          width={size}
          height={size}
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => {
            rememberThumbnailFailure(url);
            setFailedUrl(url);
          }}
        />
        {description}
      </span>
    );
  }

  const badge = BADGE[state];
  return (
    <span id={id} className={classes} data-state={state} style={style}>
      <span className="media-thumbnail__icon" role="img" aria-label={label}>
        <span aria-hidden="true">{ICON[kind]}</span>
      </span>
      {badge ? <span className="media-thumbnail__badge" aria-hidden="true">{badge}</span> : null}
      {description}
    </span>
  );
}
