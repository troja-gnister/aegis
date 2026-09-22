import type {EntrySummary} from "./types";

type FileRowProps = {
  entry: EntrySummary;
  onOpen(entry: EntrySummary): void;
};

function iconFor(entry: EntrySummary): string {
  if (entry.kind === "directory") return "▣";
  if (entry.typeHint === "image") return "▧";
  if (entry.typeHint === "video") return "▶";
  return entry.kind === "file" ? "□" : "◇";
}

function entryMetadata(entry: EntrySummary): string {
  const type = entry.kind === "directory" ? "Directory" : (entry.typeHint ?? entry.kind);
  const size = entry.size === null ? "Size unknown" : `${entry.size} bytes`;
  return entry.kind === "directory" ? type : `${type} · ${size} · ${entry.sourceState}`;
}

export function FileRow({entry, onOpen}: FileRowProps) {
  const action = entry.kind === "directory" ? "Open directory" : "Select file";
  return (
    <button
      className="file-row interactive"
      type="button"
      aria-label={`${action} ${entry.displayName}`}
      title={entry.displayName}
      onClick={() => onOpen(entry)}
    >
      <span className={`file-row__icon file-row__icon--${entry.kind}`} aria-hidden="true">
        {iconFor(entry)}
      </span>
      <span className="file-row__body">
        <span className="file-row__name" dir="auto">{entry.displayName}</span>
        <span className="file-row__metadata">{entryMetadata(entry)}</span>
      </span>
      <span className="file-row__chevron" aria-hidden="true">›</span>
    </button>
  );
}
