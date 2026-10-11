import {
  infiniteQueryOptions,
  queryOptions,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query";
import {assertQueryAuthority, captureQueryAuthority} from "../auth/authority";
import {handleCatalogFailure} from "../files/queries";
import {fetchEntryMedia, requestMediaDerivative} from "./api";
import {fetchTimeline, type TimelineInput} from "./timeline-api";
import type {
  MediaRequestResult,
  RequestableDerivativeKind,
  TimelineItem,
  TimelineKind,
  TimelinePage,
} from "./types";

/**
 * Every media query key starts with `["media", namespace]`, so an account's
 * media state is addressable per namespace and never shared with another
 * session. `purgePrivateBrowserState` clears the whole query cache, and a 401
 * cancels every `["media"]` query. New media queries (the timeline) belong
 * under the same prefix.
 */
export const mediaQueryKeys = {
  all: (namespace: string) => ["media", namespace] as const,
  details: (namespace: string, entryId: string) => ["media", namespace, "details", entryId] as const,
  timeline: (namespace: string, rootId: string, rootEpoch: number, kind: TimelineKind, year: number | null) =>
    ["media", namespace, "timeline", rootId, rootEpoch, kind, year] as const,
};

export function mediaDetailsQueryOptions(namespace: string, entryId: string) {
  return queryOptions({
    queryKey: mediaQueryKeys.details(namespace, entryId),
    queryFn: async ({signal, client}) => {
      const authority = captureQueryAuthority(namespace, signal);
      try {
        const details = await fetchEntryMedia(entryId, signal);
        assertQueryAuthority(authority, namespace, signal);
        return details;
      } catch (error) {
        return handleCatalogFailure(error, client, namespace, authority, signal);
      }
    },
    gcTime: 0,
    staleTime: 5000,
    retry: false,
  });
}

/**
 * Request an on-demand derivative for the initiating session. A 401 closes
 * private access and purges private state like every catalog request; other
 * failures (including 409 for unsupported or quarantined assets) are thrown.
 */
export async function requestMediaDerivativeForSession(
  queryClient: QueryClient,
  namespace: string,
  entryId: string,
  kind: RequestableDerivativeKind,
  signal: AbortSignal = new AbortController().signal,
): Promise<MediaRequestResult> {
  const authority = captureQueryAuthority(namespace, signal);
  try {
    const result = await requestMediaDerivative(entryId, kind, signal);
    assertQueryAuthority(authority, namespace, signal);
    return result;
  } catch (error) {
    return handleCatalogFailure(error, queryClient, namespace, authority, signal);
  }
}

/**
 * A timeline page parameter. The API pages forward only, so each parameter
 * links to the one before it: when `maxPages` drops pages from the front,
 * scrolling back can still re-fetch them (while their cursors are valid).
 * Parameters live only in memory, inside the query cache.
 */
export type TimelinePageParam = {
  cursor: string | null;
  /** Zero-based page number from the newest item. */
  index: number;
  previous: TimelinePageParam | null;
};

export const FIRST_TIMELINE_PAGE: TimelinePageParam = {cursor: null, index: 0, previous: null};
export const TIMELINE_MAX_PAGES = 5;
/**
 * Links kept behind a page parameter. Cursors expire after 900 s, so older
 * predecessors are useless and only cost memory; past the cap, the window
 * offers a restart from the newest items instead of "newer items".
 */
export const TIMELINE_CHAIN_LINKS = 20;

/** A copy of `param` whose chain of predecessors is at most `links` long. */
function cappedChain(param: TimelinePageParam, links: number): TimelinePageParam {
  if (param.previous === null) return param;
  if (links <= 0) return {...param, previous: null};
  return {...param, previous: cappedChain(param.previous, links - 1)};
}

export type TimelineData = InfiniteData<TimelinePage, TimelinePageParam>;

function deduplicateTimeline(data: TimelineData): TimelineData {
  const ids = new Set<string>();
  let changed = false;
  const pages = data.pages.map((page) => {
    const items = page.items.filter((item) => {
      if (ids.has(item.id)) return false;
      ids.add(item.id);
      return true;
    });
    if (items.length === page.items.length) return page;
    changed = true;
    return {...page, items};
  });
  return changed ? {...data, pages} : data;
}

/**
 * The keyset timeline of a root, keyed by namespace, root, root epoch and
 * filter. At most five pages are held; inactive data is dropped at once.
 */
export function timelineQueryOptions(input: TimelineInput, initialPageParam: TimelinePageParam = FIRST_TIMELINE_PAGE) {
  return infiniteQueryOptions({
    queryKey: mediaQueryKeys.timeline(input.namespace, input.rootId, input.rootEpoch, input.kind, input.year),
    initialPageParam,
    queryFn: async ({pageParam, signal, client}) => {
      const authority = captureQueryAuthority(input.namespace, signal);
      try {
        const page = await fetchTimeline(input, pageParam.cursor, signal);
        assertQueryAuthority(authority, input.namespace, signal);
        return page;
      } catch (error) {
        return handleCatalogFailure(error, client, input.namespace, authority, signal, input.rootId);
      }
    },
    getNextPageParam: (page: TimelinePage, _pages: TimelinePage[], param: TimelinePageParam) =>
      page.nextCursor === null
        ? undefined
        : {cursor: page.nextCursor, index: param.index + 1, previous: cappedChain(param, TIMELINE_CHAIN_LINKS - 1)},
    getPreviousPageParam: (_page: TimelinePage, _pages: TimelinePage[], param: TimelinePageParam) =>
      param.previous ?? undefined,
    select: deduplicateTimeline,
    maxPages: TIMELINE_MAX_PAGES,
    gcTime: 0,
    staleTime: 5000,
    retry: false,
  });
}

/** Every loaded timeline item, in order. */
export function timelineItems(data: TimelineData | undefined): TimelineItem[] {
  return data?.pages.flatMap((page) => page.items) ?? [];
}

/** The parameter of the loaded page holding `entryId`, to restart a window there. */
export function timelinePageParamFor(data: TimelineData | undefined, entryId: string): TimelinePageParam | null {
  if (!data) return null;
  const index = data.pages.findIndex((page) => page.items.some((item) => item.id === entryId));
  return index < 0 ? null : data.pageParams[index] ?? null;
}

/** The window starts below the newest items but can no longer page back to them (a capped chain). */
export function timelineWindowTruncated(data: TimelineData | undefined, hasPreviousPage: boolean): boolean {
  const first = data?.pageParams[0];
  return first !== undefined && first.index > 0 && !hasPreviousPage;
}
