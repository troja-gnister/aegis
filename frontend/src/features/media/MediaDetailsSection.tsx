import {useId} from "react";
import {Link} from "react-router";
import {ApiProblem} from "../../api/problem";
import type {SourceState} from "../files/types";
import {MEDIA_KIND_LABEL} from "./classify";
import {
  cameraText,
  displayDimensions,
  formatCameraTime,
  formatDimensions,
  formatDuration,
  wallClockDigits,
} from "./format";
import {useEntryMediaDetails} from "./hooks";
import type {CapturedAtSource, EntryMedia, MediaDetails, MediaMetadata, MediaViewerContext} from "./types";
import {canOpenInViewer, viewerLocationState, viewerPath} from "./viewer-route";
import "./media.css";

export type MediaDetailsSectionProps = {
  entry: {id: string; rootId: string; sourceState: SourceState; media: EntryMedia};
  /**
   * The opener's context for the viewer's history state. Without one, Open is
   * not offered: a guessed context would match no retained navigation record.
   */
  viewerContext?: MediaViewerContext;
  /** Called when Open is activated, before the viewer route is entered. */
  onOpenMedia?(entryId: string): void;
  /** False inside the viewer, whose toolbar already offers Download. Defaults to true. */
  showActions?: boolean;
};

const CAPTURE_SOURCE_TEXT: Record<CapturedAtSource, string> = {
  exif: "Recorded in the photo's EXIF metadata.",
  container: "Recorded in the video container.",
};

function CapturedValue({metadata}: {metadata: MediaMetadata}) {
  // Only a capture time recorded in the file is shown here, never the file modified time.
  if (metadata.capturedAt === null) return <>Capture time unknown</>;
  // A wall-clock value is the camera's own clock reading: shown as recorded, with no zone.
  const wallClock = wallClockDigits(metadata.capturedAt);
  return (
    <>
      <time dateTime={metadata.capturedAt}>{formatCameraTime(metadata.capturedAt) ?? metadata.capturedAt}</time>
      <small className="entry-details__exact">Exact: {wallClock ?? metadata.capturedAt}</small>
      {wallClock !== null ? <small>As recorded by the camera, without a time zone.</small> : null}
      {metadata.capturedAtSource ? <small>{CAPTURE_SOURCE_TEXT[metadata.capturedAtSource]}</small> : null}
    </>
  );
}

function accessNote(details: MediaDetails, canOpen: boolean, sourceState: SourceState): string | null {
  const kind = MEDIA_KIND_LABEL[details.kind].toLowerCase();
  const {preview, export: canExport} = details.permissions;
  if (!preview && !canExport) return "Your access to this file does not include viewing or downloading it.";
  if (!preview) return "Your access to this file includes downloading it, but not viewing it.";
  if (!canOpen) {
    return sourceState === "present"
      ? `This ${kind} cannot be shown in the viewer.`
      : `The original is not available right now, so this ${kind} cannot be opened.`;
  }
  return null;
}

function MediaActions({entry, details, viewerContext, onOpenMedia}: {
  entry: MediaDetailsSectionProps["entry"];
  details: MediaDetails;
  viewerContext?: MediaViewerContext;
  onOpenMedia?(entryId: string): void;
}) {
  const canOpen = canOpenInViewer(details, entry.sourceState);
  const viewerState = canOpen && viewerContext ? viewerLocationState(entry.id, viewerContext) : null;
  const downloadUrl = details.permissions.export ? details.original.downloadUrl : null;
  const note = accessNote(details, canOpen, entry.sourceState);
  const kind = MEDIA_KIND_LABEL[details.kind].toLowerCase();
  return (
    <>
      {viewerState !== null || downloadUrl !== null ? (
        <div className="media-details__actions">
          {viewerState !== null ? (
            <Link
              className="file-page-control file-page-control--primary interactive"
              to={viewerPath(entry.rootId, entry.id)}
              state={viewerState}
              onClick={() => onOpenMedia?.(entry.id)}
            >
              Open {kind}
            </Link>
          ) : null}
          {downloadUrl !== null ? (
            <a className="file-page-control interactive" href={downloadUrl} download>Download original</a>
          ) : null}
        </div>
      ) : null}
      {note !== null ? <p className="notice">{note}</p> : null}
    </>
  );
}

function mediaError(error: unknown): string {
  if (error instanceof ApiProblem && error.status === 404) {
    return "Media details are not available for this file.";
  }
  return "Media details could not be loaded.";
}

/**
 * Photo and video details for a media entry: dimensions, the capture time
 * (labeled "Captured", shown in its recorded offset and never filled from the
 * file modified time), camera and duration, plus Open (PREVIEW, when the
 * viewer can show it) and Download (EXPORT).
 */
export function MediaDetailsSection({entry, viewerContext, onOpenMedia, showActions = true}: MediaDetailsSectionProps) {
  const headingId = useId();
  const {query, details} = useEntryMediaDetails(entry.id);
  const kindLabel = MEDIA_KIND_LABEL[details?.kind ?? entry.media.kind];
  const dimensions = details ? displayDimensions(details.metadata) : null;
  const camera = details ? cameraText(details.metadata) : null;
  const notFound = query.error instanceof ApiProblem && query.error.status === 404;

  return (
    <section className="media-details" aria-labelledby={headingId}>
      <h3 id={headingId} className="media-details__title">{kindLabel} details</h3>
      {query.isPending ? <p role="status">Loading media details…</p> : null}
      {query.isError ? (
        <div className="entry-details__error">
          <p className="notice notice--error" role="alert">{mediaError(query.error)}</p>
          {!notFound ? (
            <button className="file-page-control interactive" type="button" onClick={() => void query.refetch()}>
              Retry media details
            </button>
          ) : null}
        </div>
      ) : null}
      {details ? (
        <>
          <dl className="entry-details__list">
            <div><dt>Media</dt><dd>{kindLabel}</dd></div>
            <div>
              <dt>Dimensions</dt>
              <dd>{dimensions ? formatDimensions(dimensions) : "Dimensions unknown"}</dd>
            </div>
            <div><dt>Captured</dt><dd><CapturedValue metadata={details.metadata} /></dd></div>
            {camera !== null ? <div><dt>Camera</dt><dd dir="auto">{camera}</dd></div> : null}
            {details.kind === "video" ? (
              <div>
                <dt>Duration</dt>
                <dd>
                  {details.metadata.durationMs === null
                    ? "Duration unknown"
                    : formatDuration(details.metadata.durationMs)}
                </dd>
              </div>
            ) : null}
          </dl>
          {showActions ? (
            <MediaActions
              entry={entry}
              details={details}
              viewerContext={viewerContext}
              onOpenMedia={onOpenMedia}
            />
          ) : null}
        </>
      ) : null}
    </section>
  );
}
