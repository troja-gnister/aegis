import {useInfiniteQuery, useQueryClient} from "@tanstack/react-query";
import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {Link, useLocation, useParams, useSearchParams} from "react-router";
import {ApiProblem} from "../../api/problem";
import {useAuthSession} from "../auth/session";
import type {IndexStatus} from "../files/types";
import {useIndexStatus} from "../files/useIndexStatus";
import {useAuthorizedRoot} from "../roots/useAuthorizedRoot";
import {
  FIRST_TIMELINE_PAGE,
  timelineItems,
  timelinePageParamFor,
  timelineQueryOptions,
  timelineWindowTruncated,
} from "./queries";
import {RootSectionNav} from "./RootSectionNav";
import type {TimelineInput} from "./timeline-api";
import {KIND_FILTER_ANNOUNCEMENT, KIND_FILTER_LABEL} from "./timeline-layout";
import {TimelineGrid} from "./TimelineGrid";
import type {TimelineFilter, TimelineItem, TimelineKind} from "./types";
import {
  forgetViewerReturn,
  isReturnRetained,
  peekViewerReturn,
  releaseReturnRetention,
  rememberViewerReturn,
} from "./viewer-return";
import {parseTimelineFilter, timelineSearchParameters, viewerLocationState, viewerPath} from "./viewer-route";
import "../files/files.css";
import "./photos.css";

const KINDS: readonly TimelineKind[] = ["all", "photo", "video"];

/** The server refused a cursor from an older view; only a page-one restart can continue. */
function isCursorRestart(error: unknown): boolean {
  return error instanceof ApiProblem && error.status === 409 && error.type === "cursor_restart_required";
}

function progressKey(status: IndexStatus): string {
  return [status.state, status.generation, status.observedEntries, status.lastCompletedAt ?? ""].join("|");
}

function emptyText(filter: TimelineFilter): string {
  const what = filter.kind === "photo" ? "photos" : filter.kind === "video" ? "videos" : "photos or videos";
  const when = filter.year === null ? "" : ` from ${filter.year}`;
  return `No ${what}${when} in this location yet.`;
}

/**
 * The empty timeline: says whether indexing is still running, and reloads
 * the timeline when the index makes progress (at the status poll's pace).
 */
function TimelineEmptyState({input, onProgress}: {input: TimelineInput; onProgress(): void}) {
  const status = useIndexStatus(input.namespace, input.rootId, input.rootEpoch);
  const progress = status.data ? progressKey(status.data) : null;
  const lastProgressRef = useRef<string | null>(null);
  const state = status.data?.state;

  useEffect(() => {
    if (progress === null) return;
    const previous = lastProgressRef.current;
    lastProgressRef.current = progress;
    if (previous !== null && previous !== progress) onProgress();
  }, [onProgress, progress]);

  if (state === "not_indexed") {
    return (
      <p className="notice photos-page__empty">
        This location has not been indexed yet. Photos and videos appear after the first scan.
      </p>
    );
  }
  if (state === "queued" || state === "scanning") {
    return (
      <p className="notice photos-page__empty" role="status">
        Indexing this location… Photos and videos appear here as they are found.
      </p>
    );
  }
  return <p className="photos-page__empty">{emptyText(input)}</p>;
}

/**
 * The Photos timeline of one root: every photo and video, newest first,
 * grouped by month, with a kind filter kept in the URL. Opening a tile enters
 * the viewer with the timeline as its context; Back restores focus and scroll.
 */
export function PhotosPage() {
  const {rootId = ""} = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const filter = parseTimelineFilter(searchParams);
  const {session} = useAuthSession();
  const namespace = session.cacheNamespace;
  const queryClient = useQueryClient();
  const location = useLocation();
  const routeKey = location.key;
  const [announcement, setAnnouncement] = useState("");
  const [placeExpired, setPlaceExpired] = useState(false);
  const [quietRestart, setQuietRestart] = useState(false);
  const [restarted, setRestarted] = useState(false);
  const [resetRequest, setResetRequest] = useState(0);
  // A viewer opened from this history entry leaves where to return; a filter
  // change replaces the entry (a new route key), which retires the record.
  // Back through the session check finds the window held for it (a retention); otherwise
  // the window restarts at the stored page.
  const [mounted] = useState(() => ({
    routeKey,
    record: peekViewerReturn(namespace, routeKey),
    retained: isReturnRetained(namespace, routeKey),
  }));
  const returnRecord = mounted.routeKey === routeKey ? mounted.record : null;
  const retained = mounted.routeKey === routeKey && mounted.retained;
  const openingViewerRef = useRef(false);
  const startParam = !restarted && returnRecord?.timelinePageParam ? returnRecord.timelinePageParam : FIRST_TIMELINE_PAGE;

  const {rootQuery, root, sessionExpiring} = useAuthorizedRoot(rootId);
  const input: TimelineInput = {
    namespace,
    rootId,
    rootEpoch: root?.authorizationEpoch ?? 0,
    kind: filter.kind,
    year: filter.year,
  };
  const options = timelineQueryOptions(input, startParam);
  const timelineQuery = useInfiniteQuery({
    ...options,
    enabled: Boolean(root),
    refetchOnWindowFocus: false,
    // A retained window is taken back as is: refetching it would re-request every page.
    ...(retained ? {refetchOnMount: false} : {}),
  });
  const items = useMemo(() => timelineItems(timelineQuery.data), [timelineQuery.data]);
  const keyText = JSON.stringify(options.queryKey);
  const keyRef = useRef(options.queryKey);
  const restartRequired = timelineQuery.isError && isCursorRestart(timelineQuery.error);
  const {fetchNextPage, fetchPreviousPage, refetch, isFetching} = timelineQuery;

  useEffect(() => {
    keyRef.current = options.queryKey;
  });

  useEffect(() => {
    // This page's observer now holds the window (it subscribed above).
    if (retained) releaseReturnRetention(namespace, mounted.routeKey);
  }, [mounted.routeKey, namespace, retained]);

  useEffect(() => {
    const key = JSON.parse(keyText) as unknown[];
    // Leaving Photos, or changing the filter, cancels in-flight timeline pages, except when
    // a tile opens the viewer, which adopts this very query in the same commit.
    return () => {
      if (!openingViewerRef.current) void queryClient.cancelQueries({queryKey: key, exact: true});
    };
  }, [keyText, queryClient]);

  useEffect(() => {
    if (resetRequest === 0) return;
    void queryClient.resetQueries({queryKey: keyRef.current, exact: true});
  }, [queryClient, resetRequest]);

  // A stored place whose cursor expired restarts quietly from the newest items.
  const placeRefused = restartRequired && returnRecord !== null && !restarted && items.length === 0;

  useEffect(() => {
    if (!placeRefused) return;
    setRestarted(true);
    setPlaceExpired(true);
    setQuietRestart(true);
    setAnnouncement("Your place expired; showing the newest items.");
    setResetRequest((count) => count + 1);
  }, [placeRefused]);

  useEffect(() => {
    // The quiet restart ends once the refused window has been reset.
    if (quietRestart && !restartRequired) setQuietRestart(false);
  }, [quietRestart, restartRequired]);

  const loadNext = useCallback(() => {
    if (!isFetching) void fetchNextPage({cancelRefetch: false});
  }, [fetchNextPage, isFetching]);
  const loadPrevious = useCallback(() => {
    if (!isFetching) void fetchPreviousPage({cancelRefetch: false});
  }, [fetchPreviousPage, isFetching]);
  const reloadAfterProgress = useCallback(() => {
    if (!isFetching) void refetch();
  }, [isFetching, refetch]);
  const restoreDone = useCallback(() => {
    forgetViewerReturn(namespace, mounted.routeKey);
  }, [mounted.routeKey, namespace]);

  const setFilter = (next: TimelineFilter) => {
    setAnnouncement(next.kind === filter.kind && next.year === null
      ? "Showing every year."
      : KIND_FILTER_ANNOUNCEMENT[next.kind]);
    // The filter lives in the URL only; replacing keeps Back meaning "leave Photos".
    setSearchParams(timelineSearchParameters(next), {replace: true});
  };

  const restartFromBeginning = () => {
    setRestarted(true);
    setPlaceExpired(false);
    setAnnouncement("Starting from the newest items…");
    setResetRequest((count) => count + 1);
  };

  if (rootQuery.isPending || sessionExpiring) {
    return <section className="photos-page"><p role="status">Loading root…</p></section>;
  }
  if (rootQuery.isError) {
    return (
      <section className="photos-page">
        <h1>Photos unavailable</h1>
        <p className="notice notice--error" role="alert">The authorized root could not be loaded.</p>
      </section>
    );
  }
  if (!root) {
    return (
      <section className="photos-page files-page--empty">
        <p className="eyebrow">Library</p>
        <h1>Root unavailable</h1>
        <p>This root is not assigned to this account.</p>
        <Link className="file-page-control interactive" to="/roots">Return to roots</Link>
      </section>
    );
  }

  const canPreview = root.permissions.includes("preview");
  const truncated = timelineWindowTruncated(timelineQuery.data, Boolean(timelineQuery.hasPreviousPage));
  const restore = returnRecord && !restarted ? {
    entryId: returnRecord.entryId,
    offset: returnRecord.entryId === returnRecord.openedEntryId ? returnRecord.anchorOffset : null,
  } : undefined;
  // Without PREVIEW the viewer could show nothing, so tiles are not links.
  const viewerTarget = (item: TimelineItem) => canPreview ? {
    to: viewerPath(root.id, item.id),
    state: viewerLocationState(item.id, {
      source: "timeline", rootId: root.id, returnKey: routeKey, kind: filter.kind, year: filter.year,
    }),
  } : null;
  const openItem = (item: TimelineItem, anchorOffset: number) => {
    openingViewerRef.current = true;
    rememberViewerReturn(namespace, routeKey, {
      entryId: item.id,
      openedEntryId: item.id,
      anchorOffset,
      timelinePageParam: timelinePageParamFor(timelineQuery.data, item.id),
    });
  };
  const fullError = timelineQuery.isError && !timelineQuery.isFetchNextPageError &&
    !timelineQuery.isFetchPreviousPageError;

  return (
    <section className="photos-page">
      <nav className="files-page__breadcrumbs" aria-label="Photos location">
        <Link className="interactive" to="/roots">Roots</Link>
      </nav>
      <p className="eyebrow">Read-only library</p>
      <h1 dir="auto">{root.displayName}</h1>
      <RootSectionNav rootId={root.id} current="photos" />
      <div className="photos-toolbar">
        <div className="photos-filter" role="group" aria-label="Show">
          {KINDS.map((kind) => (
            <button
              key={kind}
              className="interactive"
              type="button"
              aria-pressed={filter.kind === kind}
              onClick={() => { if (filter.kind !== kind) setFilter({...filter, kind}); }}
            >
              {KIND_FILTER_LABEL[kind]}
            </button>
          ))}
        </div>
        {filter.year !== null ? (
          <button
            className="filter-chip filter-chip--clear interactive"
            type="button"
            onClick={() => setFilter({...filter, year: null})}
          >
            {filter.year} · Show every year
          </button>
        ) : null}
      </div>
      <p className="visually-hidden" role="status" aria-live="polite">{announcement}</p>
      {!canPreview ? (
        <p className="notice photos-page__empty">
          Your access to this location does not include viewing photos and videos.
        </p>
      ) : null}
      {placeExpired ? <p className="notice photos-page__empty">Your place expired; showing the newest items.</p> : null}
      {timelineQuery.isPending ? <p role="status">Loading photos…</p> : null}
      {placeRefused || quietRestart ? null : restartRequired ? (
        <div className="photos-page__state">
          <p className="notice notice--error" role="alert">
            This timeline changed. Start from the newest items to keep browsing.
          </p>
          <button className="file-page-control interactive" type="button" onClick={restartFromBeginning}>
            Start from the newest items
          </button>
        </div>
      ) : fullError ? (
        <div className="photos-page__state">
          <p className="notice notice--error" role="alert">Photos could not be loaded.</p>
          <button className="file-page-control interactive" type="button" onClick={() => void refetch()}>
            Try again
          </button>
        </div>
      ) : timelineQuery.isFetchNextPageError || timelineQuery.isFetchPreviousPageError ? (
        <div className="photos-page__state">
          <p className="notice notice--error" role="alert">
            {timelineQuery.isFetchNextPageError ? "Older items could not be loaded." : "Newer items could not be loaded."}
          </p>
          <button
            className="file-page-control interactive"
            type="button"
            onClick={() => (timelineQuery.isFetchNextPageError ? loadNext() : loadPrevious())}
          >
            Try again
          </button>
        </div>
      ) : null}
      {timelineQuery.isSuccess && items.length === 0 ? (
        <TimelineEmptyState input={input} onProgress={reloadAfterProgress} />
      ) : null}
      {truncated && !restartRequired ? (
        <div className="photos-page__state">
          <button className="file-page-control interactive" type="button" onClick={restartFromBeginning}>
            Show the newest items
          </button>
        </div>
      ) : null}
      {items.length > 0 ? (
        <TimelineGrid
          // A filter or restart begins a fresh window at the top.
          key={`${filter.kind}:${filter.year ?? ""}:${resetRequest}`}
          items={items}
          // After a failure, paging waits for Try again instead of retrying on its own.
          hasNext={Boolean(timelineQuery.hasNextPage) && !restartRequired && !timelineQuery.isFetchNextPageError}
          hasPrevious={Boolean(timelineQuery.hasPreviousPage) && !restartRequired &&
            !timelineQuery.isFetchPreviousPageError}
          loadingNext={timelineQuery.isFetchingNextPage}
          loadingPrevious={timelineQuery.isFetchingPreviousPage}
          onLoadNext={loadNext}
          onLoadPrevious={loadPrevious}
          viewerTarget={viewerTarget}
          onOpen={openItem}
          restore={restore}
          onRestored={restoreDone}
        />
      ) : null}
    </section>
  );
}
