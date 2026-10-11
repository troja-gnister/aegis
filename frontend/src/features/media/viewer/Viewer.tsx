import {useQueries, useQuery} from "@tanstack/react-query";
import {useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode} from "react";
import {ApiProblem} from "../../../api/problem";
import {useAuthSession} from "../../auth/session";
import {entryQueryOptions} from "../../files/queries";
import {MEDIA_KIND_LABEL} from "../classify";
import {useEntryMediaDetails} from "../hooks";
import {MediaDetailsSection} from "../MediaDetailsSection";
import {mediaDetailsQueryOptions} from "../queries";
import type {MediaKind} from "../types";
import {trapTabKey, useModalDialog} from "./dialog";
import {PhotoStage} from "./PhotoStage";
import {neighborImageUrl, useNeighborImagePrefetch} from "./prefetch";
import {VideoStage} from "./VideoStage";
import {viewerItemOf, type ViewerItem, type ViewerList} from "./viewer-list";
import "../../files/files.css";
import "./viewer.css";

export type ViewerProps = {
  rootId: string;
  entryId: string;
  list: ViewerList;
  /** Show another entry of the context (replacing the history entry). */
  onNavigate(entryId: string): void;
  onClose(): void;
};

function kindText(kind: MediaKind | null | undefined): string {
  return kind ? MEDIA_KIND_LABEL[kind].toLowerCase() : "item";
}

function isNotFound(error: unknown): boolean {
  return error instanceof ApiProblem && error.status === 404;
}

/** Keys the viewer handles itself; a focused video keeps its own arrow-key seeking. */
function ownsArrowKeys(target: EventTarget | null): boolean {
  return !(target instanceof Element && target.closest("video, input, textarea, select"));
}

function ViewerFrame({titleId, title, resetKey, onClose, onKeyDown, toolbar, children}: {
  titleId: string;
  title: ReactNode;
  resetKey: string;
  onClose(): void;
  onKeyDown?(event: KeyboardEvent<HTMLDialogElement>): boolean;
  toolbar?: ReactNode;
  children: ReactNode;
}) {
  const dialogRef = useModalDialog<HTMLDialogElement>(resetKey);
  return (
    <dialog
      ref={dialogRef}
      className="viewer"
      aria-modal="true"
      aria-labelledby={titleId}
      tabIndex={-1}
      onCancel={(event) => {
        // A close request (Escape not handled below, or the system Back gesture on Android)
        // closes the route, not just the element: the history entry must go too.
        event.preventDefault();
        onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === "Tab") {
          trapTabKey(event, dialogRef.current);
          return;
        }
        if (onKeyDown?.(event)) return;
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
        }
      }}
    >
      <header className="viewer__toolbar">
        <button className="viewer-button viewer-button--icon interactive" type="button" onClick={onClose}
          aria-label="Close viewer">
          <span aria-hidden="true">✕</span>
        </button>
        <div className="viewer__heading">{title}</div>
        {toolbar}
      </header>
      {children}
    </dialog>
  );
}

/** A viewer that cannot show anything: the reason, and a way back. */
export function ViewerMessage({message, onClose, loading = false}: {
  message: string;
  onClose(): void;
  loading?: boolean;
}) {
  const titleId = useId();
  return (
    <ViewerFrame
      titleId={titleId}
      title={<h2 id={titleId} className="viewer__title">Viewer</h2>}
      resetKey={message}
      onClose={onClose}
    >
      <div className="viewer__body">
        <div className="viewer-stage__status viewer-stage__status--center">
          <p role={loading ? "status" : "alert"}>{message}</p>
        </div>
      </div>
    </ViewerFrame>
  );
}

/**
 * The full-screen photo and video viewer: a modal dialog over the current
 * context list (a directory window or the timeline). Left and right (keys,
 * swipes or buttons) move through the context, Escape closes, and the
 * toolbar shows the position when the whole list is known, Download with
 * EXPORT, and an information panel.
 */
export function Viewer({rootId, entryId, list, onNavigate, onClose}: ViewerProps) {
  const {session} = useAuthSession();
  const namespace = session.cacheNamespace;
  const titleId = useId();
  const infoId = useId();
  const infoButtonRef = useRef<HTMLButtonElement>(null);
  const openedRef = useRef(entryId);
  const movedRef = useRef(false);
  const [infoOpen, setInfoOpen] = useState(false);

  const index = list.items.findIndex((item) => item.id === entryId);
  const listed = index >= 0 ? list.items[index] : undefined;
  // The entry's own details are fetched only when the settled context list does not hold it.
  const entryQuery = useQuery({
    ...entryQueryOptions(namespace, entryId),
    enabled: listed === undefined && !list.pending,
    refetchOnWindowFocus: false,
  });
  const fetchedEntry = entryQuery.data?.id === entryId ? entryQuery.data : undefined;
  const fetched = fetchedEntry && fetchedEntry.rootId === rootId ? viewerItemOf(fetchedEntry) : null;
  const current: ViewerItem | undefined = listed ?? fetched ?? undefined;
  const {query: detailsQuery, details} = useEntryMediaDetails(entryId);
  const previous = index > 0 ? list.items[index - 1] : undefined;
  const next = index >= 0 ? list.items[index + 1] : undefined;
  const kind = details?.kind ?? current?.media.kind ?? null;
  const position = list.complete && index >= 0 ? {number: index + 1, total: list.items.length} : null;
  const downloadUrl = details?.permissions.export ? details.original.downloadUrl : null;
  const neighbors = [previous, next].filter((item): item is ViewerItem => item !== undefined);
  const neighborDetails = useQueries({
    queries: neighbors.map((item) => ({
      ...mediaDetailsQueryOptions(namespace, item.id),
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    })),
  });
  useNeighborImagePrefetch(neighborDetails.map((result) => neighborImageUrl(result.data)));

  const {
    hasNext, hasPrevious, loadingNext, loadingPrevious, nextFailed, previousFailed, loadNext, loadPrevious,
    rememberShown,
  } = list;
  const itemCount = list.items.length;
  const attemptedEdgesRef = useRef(new Set<string>());

  useEffect(() => {
    // The neighbor beyond a window edge is fetched with the next (or previous) page, at most
    // once per edge item: a page that adds no media, or a failure, never starts a loop.
    if (index < 0) return;
    const attempted = attemptedEdgesRef.current;
    if (index >= itemCount - 1 && hasNext && !loadingNext && !nextFailed && !attempted.has(`next:${entryId}`)) {
      if (loadNext()) attempted.add(`next:${entryId}`);
    } else if (index === 0 && hasPrevious && !loadingPrevious && !previousFailed &&
               !attempted.has(`previous:${entryId}`)) {
      if (loadPrevious()) attempted.add(`previous:${entryId}`);
    }
  }, [
    entryId, hasNext, hasPrevious, index, itemCount, loadNext, loadPrevious, loadingNext, loadingPrevious,
    nextFailed, previousFailed,
  ]);

  useEffect(() => {
    if (entryId !== openedRef.current) movedRef.current = true;
    rememberShown(entryId, movedRef.current);
  }, [entryId, index, rememberShown]);

  const show = (item: ViewerItem | undefined) => {
    if (item) onNavigate(item.id);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDialogElement>): boolean => {
    if (event.key === "Escape" && infoOpen) {
      event.preventDefault();
      setInfoOpen(false);
      infoButtonRef.current?.focus({preventScroll: true});
      return true;
    }
    if (event.altKey || event.ctrlKey || event.metaKey || !ownsArrowKeys(event.target)) return false;
    if (event.key === "ArrowRight" && next) {
      event.preventDefault();
      show(next);
      return true;
    }
    if (event.key === "ArrowLeft" && previous) {
      event.preventDefault();
      show(previous);
      return true;
    }
    return false;
  };

  const kindLabel = kind ? MEDIA_KIND_LABEL[kind] : "Media";
  const name = current?.displayName ?? null;
  const notFound = isNotFound(detailsQuery.error) || isNotFound(entryQuery.error);
  const notMedia = fetchedEntry !== undefined && fetched === null;
  const detailsFailed = detailsQuery.isError && details === undefined;
  const thumbnailUrl = current?.media.thumbnailUrl ?? null;
  const dimensions = details?.metadata.width && details.metadata.height
    ? {width: details.metadata.width, height: details.metadata.height}
    : current?.media.width && current.media.height
      ? {width: current.media.width, height: current.media.height}
      : null;

  let stage: ReactNode;
  if (notFound || notMedia) {
    stage = (
      <div className="viewer-stage__status viewer-stage__status--center">
        <p role="alert">This file is not available in the viewer.</p>
      </div>
    );
  } else if (detailsFailed) {
    stage = (
      <div className="viewer-stage__status viewer-stage__status--center">
        <p role="alert">The {kindText(kind)} could not be loaded.</p>
        <button className="viewer-button interactive" type="button" onClick={() => void detailsQuery.refetch()}>
          Try again
        </button>
      </div>
    );
  } else if (kind === "video") {
    stage = (
      <VideoStage
        key={entryId}
        entryId={entryId}
        details={details}
        detailsFailed={detailsFailed}
        refetchDetails={detailsQuery.refetch}
        thumbnailUrl={thumbnailUrl}
        title={name ?? "Video"}
      />
    );
  } else if (kind === "photo") {
    stage = (
      <PhotoStage
        key={entryId}
        entryId={entryId}
        details={details}
        detailsFailed={detailsFailed}
        refetchDetails={detailsQuery.refetch}
        thumbnailUrl={thumbnailUrl}
        dimensions={dimensions}
        alt={name ?? "Photo"}
        onSwipe={(direction) => show(direction === "next" ? next : previous)}
      />
    );
  } else {
    stage = (
      <div className="viewer-stage__status viewer-stage__status--center">
        <p role="status">Loading…</p>
      </div>
    );
  }

  const positionText = position ? `${position.number} of ${position.total}` : null;

  return (
    <ViewerFrame
      titleId={titleId}
      title={(
        <>
          <h2 id={titleId} className="viewer__title" dir="auto">{name ?? `${kindLabel} viewer`}</h2>
          {positionText ? <p className="viewer__position">{positionText}</p> : null}
        </>
      )}
      resetKey={entryId}
      onClose={onClose}
      onKeyDown={handleKeyDown}
      toolbar={(
        <div className="viewer__actions">
          {downloadUrl !== null ? (
            <a className="viewer-button interactive" href={downloadUrl} download>Download original</a>
          ) : null}
          <button
            ref={infoButtonRef}
            className="viewer-button interactive"
            type="button"
            aria-expanded={infoOpen}
            aria-controls={infoId}
            onClick={() => setInfoOpen((open) => !open)}
          >
            Info
          </button>
        </div>
      )}
    >
      <div className="viewer__body">
        <div className="viewer__stage">{stage}</div>
        {previous ? (
          <button
            className="viewer-button viewer-button--icon viewer__nav viewer__nav--previous interactive"
            type="button"
            aria-label={`Previous ${kindText(previous.media.kind)}`}
            onClick={() => show(previous)}
          >
            <span aria-hidden="true">‹</span>
          </button>
        ) : null}
        {next ? (
          <button
            className="viewer-button viewer-button--icon viewer__nav viewer__nav--next interactive"
            type="button"
            aria-label={`Next ${kindText(next.media.kind)}`}
            onClick={() => show(next)}
          >
            <span aria-hidden="true">›</span>
          </button>
        ) : null}
        {nextFailed || previousFailed ? (
          <div className="viewer__notice">
            <p role="status">More items could not be loaded.</p>
            <button
              className="viewer-button interactive"
              type="button"
              onClick={() => (nextFailed ? loadNext() : loadPrevious())}
            >
              Retry loading items
            </button>
          </div>
        ) : null}
        <aside id={infoId} className="viewer__info" aria-label={`${kindLabel} information`} hidden={!infoOpen}>
          {current ? (
            <MediaDetailsSection
              entry={{id: current.id, rootId: current.rootId, sourceState: current.sourceState, media: current.media}}
              showActions={false}
            />
          ) : null}
        </aside>
      </div>
      <p className="visually-hidden" aria-live="polite">
        {name ? `${name}${positionText ? `, ${positionText}` : ""}` : ""}
      </p>
    </ViewerFrame>
  );
}
