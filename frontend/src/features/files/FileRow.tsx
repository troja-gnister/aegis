import {useId} from "react";
import {mediaKindOf} from "../media/classify";
import {MediaThumbnail} from "../media/MediaThumbnail";
import type {EntrySummary} from "./types";

type FileRowProps = {
  entry: EntrySummary;
  onOpen(entry: EntrySummary): void;
  /** Root-wide results: the name of the folder holding the entry. */
  location?: string | null;
  /** What activating the row does, when it differs from the kind's default. */
  action?: string;
};

function iconFor(entry: EntrySummary): string {
  if (entry.kind === "directory") return "▣";
  return entry.kind === "file" ? "□" : "◇";
}

function entryMetadata(entry: EntrySummary): string {
  const type = entry.kind === "directory" ? "Directory" : (entry.typeHint ?? entry.kind);
  const size = entry.size === null ? "Size unknown" : `${entry.size} bytes`;
  return entry.kind === "directory" ? type : `${type} · ${size} · ${entry.sourceState}`;
}

export function FileRow({entry, onOpen, location = null, action}: FileRowProps) {
  const thumbnailDescriptionId = useId();
  const mediaKind = mediaKindOf(entry);
  const verb = action ?? (entry.kind === "directory" ? "Open directory" : "Select file");
  const place = location === null ? "" : ` in ${location}`;
  return (
    <button
      className={`file-row${location === null ? "" : " file-row--located"} interactive`}
      type="button"
      aria-label={`${verb} ${entry.displayName}${place}`}
      // A button's content is presentational; a hidden text node describes the thumbnail state.
      aria-describedby={mediaKind ? thumbnailDescriptionId : undefined}
      title={entry.displayName}
      onClick={() => onOpen(entry)}
    >
      {mediaKind ? (
        <span className="file-row__icon file-row__icon--media">
          <MediaThumbnail entry={entry} descriptionId={thumbnailDescriptionId} />
        </span>
      ) : (
        <span className={`file-row__icon file-row__icon--${entry.kind}`} aria-hidden="true">
          {iconFor(entry)}
        </span>
      )}
      <span className="file-row__body">
        <span className="file-row__name" dir="auto">{entry.displayName}</span>
        {location === null ? null : (
          <span className="file-row__location">in <bdi>{location}</bdi></span>
        )}
        <span className="file-row__metadata">{entryMetadata(entry)}</span>
      </span>
      <span className="file-row__chevron" aria-hidden="true">›</span>
    </button>
  );
}
