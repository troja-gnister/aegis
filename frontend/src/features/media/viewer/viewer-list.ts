import {useInfiniteQuery, useQueryClient, type QueryKey} from "@tanstack/react-query";
import {useCallback, useEffect, useMemo, useRef} from "react";
import {ApiProblem} from "../../../api/problem";
import type {FileNavigation} from "../../files/navigation";
import {directoryQueryOptions} from "../../files/queries";
import type {BrowseInput, EntrySummary, SourceState} from "../../files/types";
import {TIMELINE_MAX_PAGES, timelineItems, timelinePageParamFor, timelineQueryOptions} from "../queries";
import type {TimelineInput} from "../timeline-api";
import type {EntryMedia, MediaViewerContext} from "../types";
import {releaseReturnRetention, retainForReturn, updateViewerReturn} from "../viewer-return";

/** What the viewer needs to know about an entry of its context list. */
export type ViewerItem = {
  id: string;
  rootId: string;
  displayName: string;
  sourceState: SourceState;
  media: EntryMedia;
};

/**
 * The viewer's context list: the media entries of the directory window or
 * the timeline window it was opened from, read from the same infinite query
 * (and so the same cache entry) as the opener.
 */
export type ViewerList = {
  items: ViewerItem[];
  /**
   * More items exist in that direction and may be fetched: false at the end,
   * after a refused (expired) cursor, and when fetching would make `maxPages`
   * drop the page that holds the current entry.
   */
  hasNext: boolean;
  hasPrevious: boolean;
  loadingNext: boolean;
  loadingPrevious: boolean;
  /** The last fetch in that direction failed and may be retried. */
  nextFailed: boolean;
  previousFailed: boolean;
  /** Start fetching a page; false when nothing was started. */
  loadNext(): boolean;
  loadPrevious(): boolean;
  /** The first page of the context is still loading. */
  pending: boolean;
  /** Both ends are loaded, so "n of total" is exact. */
  complete: boolean;
  /**
   * Record that the viewer shows `entryId`, so Back restores focus and the
   * window there. `moved` is false while the opened entry was never left.
   */
  rememberShown(entryId: string, moved: boolean): void;
};

type DirectoryContext = Extract<MediaViewerContext, {source: "directory"}>;
type TimelineContext = Extract<MediaViewerContext, {source: "timeline"}>;

/** A media file the viewer can show: a regular file the catalog describes as media. */
export function viewerItemOf(entry: Pick<EntrySummary, "id" | "rootId" | "displayName" | "sourceState" | "kind" | "media">): ViewerItem | null {
  if (entry.kind !== "file" || entry.media === null) return null;
  return {
    id: entry.id,
    rootId: entry.rootId,
    displayName: entry.displayName,
    sourceState: entry.sourceState,
    media: entry.media,
  };
}

const noop = () => undefined;

/** The list of a viewer opened without a context (a reload's leftovers, a pasted link): no neighbors. */
export const STANDALONE_VIEWER_LIST: ViewerList = {
  items: [],
  hasNext: false,
  hasPrevious: false,
  loadingNext: false,
  loadingPrevious: false,
  nextFailed: false,
  previousFailed: false,
  loadNext: () => false,
  loadPrevious: () => false,
  pending: false,
  complete: false,
  rememberShown: noop,
};

function isCursorRestart(error: unknown): boolean {
  return error instanceof ApiProblem && error.status === 409 && error.type === "cursor_restart_required";
}

type WindowQuery = {
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  isFetchingNextPage: boolean;
  isFetchingPreviousPage: boolean;
  isFetchNextPageError: boolean;
  isFetchPreviousPageError: boolean;
  isFetching: boolean;
  isPending: boolean;
  isSuccess: boolean;
  error: unknown;
  fetchNextPage(options: {cancelRefetch: boolean}): Promise<unknown>;
  fetchPreviousPage(options: {cancelRefetch: boolean}): Promise<unknown>;
};

/**
 * Paging controls shared by both contexts. `currentPage` is the index of the
 * loaded page that holds the current entry (or -1), `pageCount` the number of
 * loaded pages. A refused cursor ends the context in that direction.
 */
function useWindowControls(query: WindowQuery, currentPage: number, pageCount: number, maxPages: number) {
  const {fetchNextPage, fetchPreviousPage, isFetching} = query;
  const atCapacity = pageCount >= maxPages;
  const nextEnded = query.isFetchNextPageError && isCursorRestart(query.error);
  const previousEnded = query.isFetchPreviousPageError && isCursorRestart(query.error);
  // Fetching forward drops the first page, backward the last one; never the current entry's page.
  const nextBlocked = atCapacity && currentPage === 0;
  const previousBlocked = atCapacity && currentPage === pageCount - 1;
  const loadNext = useCallback(() => {
    if (isFetching) return false;
    void fetchNextPage({cancelRefetch: false});
    return true;
  }, [fetchNextPage, isFetching]);
  const loadPrevious = useCallback(() => {
    if (isFetching) return false;
    void fetchPreviousPage({cancelRefetch: false});
    return true;
  }, [fetchPreviousPage, isFetching]);
  return {
    hasNext: query.hasNextPage && !nextEnded && !nextBlocked,
    hasPrevious: query.hasPreviousPage && !previousEnded && !previousBlocked,
    loadingNext: query.isFetchingNextPage,
    loadingPrevious: query.isFetchingPreviousPage,
    nextFailed: query.isFetchNextPageError && !nextEnded,
    previousFailed: query.isFetchPreviousPageError && !previousEnded,
    loadNext,
    loadPrevious,
    pending: query.isPending,
    complete: query.isSuccess && !query.hasNextPage && !query.hasPreviousPage,
  };
}

/**
 * When the viewer unmounts (closing through Back unmounts it behind the
 * session check), hold the context's list query so the opener gets its
 * window back instead of fetching it again.
 */
function useRetainOnUnmount(options: {queryKey: QueryKey}, namespace: string, returnKey: string): void {
  const queryClient = useQueryClient();
  const optionsRef = useRef(options);

  useEffect(() => {
    optionsRef.current = options;
  });

  useEffect(() => {
    // The viewer's own observer holds the window while it is mounted.
    releaseReturnRetention(namespace, returnKey);
    return () => retainForReturn(queryClient, optionsRef.current, namespace, returnKey);
  }, [namespace, queryClient, returnKey]);
}

/**
 * The directory window the viewer was opened from. The directory's filters,
 * sort and window cursor are recalled from the opener's in-memory navigation
 * record, so the query key (and cached pages) match the file list's.
 */
export function useDirectoryViewerList(
  context: DirectoryContext,
  rootEpoch: number,
  namespace: string,
  navigation: FileNavigation | null,
  entryId: string,
): ViewerList {
  const record = useMemo(() => navigation?.recall(context.returnKey), [context.returnKey, navigation]);
  const usable = record && record.rootId === context.rootId && record.parentId === context.parentId
    ? record
    : undefined;
  const input: BrowseInput = {
    namespace,
    rootId: context.rootId,
    rootEpoch,
    parentId: context.parentId,
    filters: usable?.filters ?? {v: 1},
    sort: usable?.sort ?? "name",
    order: usable?.order ?? "asc",
    cursor: usable?.cursor ?? null,
  };
  const options = directoryQueryOptions(input);
  const query = useInfiniteQuery({
    ...options,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  });
  // The query is subscribed above, so its own observer is released first on unmount.
  useRetainOnUnmount(options, namespace, context.returnKey);
  const {data} = query;
  const items = useMemo(
    () => (data?.pages ?? []).flatMap((page) => page.entries)
      .map(viewerItemOf)
      .filter((item): item is ViewerItem => item !== null),
    [data],
  );
  const pages = data?.pages ?? [];
  const currentPage = pages.findIndex((page) => page.entries.some((entry) => entry.id === entryId));
  const controls = useWindowControls(query, currentPage, pages.length, options.maxPages ?? 5);
  const rememberShown = useCallback((shownId: string, moved: boolean) => {
    updateViewerReturn(namespace, context.returnKey, {entryId: shownId});
    if (!moved || !navigation || !usable || !data) return;
    const pageIndex = data.pages.findIndex((page) => page.entries.some((entry) => entry.id === shownId));
    if (pageIndex < 0) return;
    const pageParam = data.pageParams[pageIndex];
    // Back re-opens the file list at the page and row of the entry shown last.
    try {
      navigation.remember(context.returnKey, {
        ...usable,
        cursor: typeof pageParam === "string" ? pageParam : null,
        visibleAnchorId: shownId,
        visibleAnchorOffset: 0,
      });
    } catch {
      // An oversized record keeps the opener's own anchor; Back still works.
    }
  }, [context.returnKey, data, namespace, navigation, usable]);

  return {items, ...controls, rememberShown};
}

/** The timeline window the viewer was opened from, by its route filter. */
export function useTimelineViewerList(
  context: TimelineContext,
  rootEpoch: number,
  namespace: string,
  entryId: string,
): ViewerList {
  const input: TimelineInput = {
    namespace,
    rootId: context.rootId,
    rootEpoch,
    kind: context.kind,
    year: context.year,
  };
  const options = timelineQueryOptions(input);
  const query = useInfiniteQuery({
    ...options,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  });
  useRetainOnUnmount(options, namespace, context.returnKey);
  const {data} = query;
  const items = useMemo(
    () => timelineItems(data).map(viewerItemOf).filter((item): item is ViewerItem => item !== null),
    [data],
  );
  const pages = data?.pages ?? [];
  const currentPage = pages.findIndex((page) => page.items.some((item) => item.id === entryId));
  const controls = useWindowControls(query, currentPage, pages.length, TIMELINE_MAX_PAGES);
  const rememberShown = useCallback((shownId: string) => {
    const pageParam = timelinePageParamFor(data, shownId);
    updateViewerReturn(namespace, context.returnKey, pageParam === null ? {entryId: shownId} : {entryId: shownId, timelinePageParam: pageParam});
  }, [context.returnKey, data, namespace]);

  return {items, ...controls, rememberShown};
}
