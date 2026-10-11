import {InfiniteQueryObserver, type QueryClient, type QueryKey} from "@tanstack/react-query";
import {capturePrivateState, isPrivateStateCurrent, registerPrivateStateCleanup} from "../auth/cache";
import type {TimelinePageParam} from "./queries";

/**
 * Where to put focus and scroll back when the viewer closes. Records are kept
 * in memory only, keyed by the cache namespace and the opener's route key
 * (the viewer state's `returnKey`), and they are forgotten with the rest of
 * the private state on logout or an account switch.
 */
export type ViewerReturn = {
  /** The entry the viewer showed last; the opener focuses it. */
  entryId: string;
  /** The entry the opener opened. */
  openedEntryId: string;
  /**
   * The opened entry's row offset from the top of the opener's scroll
   * viewport, so an unmoved return restores the exact scroll position.
   */
  anchorOffset: number | null;
  /** Timeline only: the page holding `entryId`, to restart a dropped window there. */
  timelinePageParam: TimelinePageParam | null;
};

const MAX_RETURNS = 32;
const returns = new Map<string, ViewerReturn>();
registerPrivateStateCleanup(() => {
  returns.clear();
  releaseAllReturnRetentions();
});

function storeKey(namespace: string, returnKey: string): string {
  return JSON.stringify([namespace, returnKey]);
}

function store(key: string, record: ViewerReturn): void {
  returns.delete(key);
  returns.set(key, record);
  while (returns.size > MAX_RETURNS) {
    const oldest = returns.keys().next().value as string | undefined;
    if (oldest === undefined) break;
    returns.delete(oldest);
  }
}

/** Record what an opener opened, before entering the viewer. */
export function rememberViewerReturn(namespace: string, returnKey: string, record: ViewerReturn): void {
  store(storeKey(namespace, returnKey), {...record});
}

/**
 * Record the entry the viewer now shows. A record is created for an opener
 * that did not leave one (the directory details panel).
 */
export function updateViewerReturn(
  namespace: string,
  returnKey: string,
  update: {entryId: string; timelinePageParam?: TimelinePageParam | null},
): void {
  const key = storeKey(namespace, returnKey);
  const existing = returns.get(key);
  store(key, {
    entryId: update.entryId,
    openedEntryId: existing?.openedEntryId ?? update.entryId,
    anchorOffset: existing?.anchorOffset ?? null,
    timelinePageParam: update.timelinePageParam === undefined
      ? existing?.timelinePageParam ?? null
      : update.timelinePageParam,
  });
}

/** The return record for an opener route, without consuming it (safe in render). */
export function peekViewerReturn(namespace: string, returnKey: string): ViewerReturn | null {
  const record = returns.get(storeKey(namespace, returnKey));
  return record ? {...record} : null;
}

/** Forget an opener's return record once its focus and scroll were restored. */
export function forgetViewerReturn(namespace: string, returnKey: string): void {
  returns.delete(storeKey(namespace, returnKey));
}

/*
 * Back from the viewer is a history POP, and the session check that guards it
 * unmounts private content until it succeeds. The opener's list query has
 * gcTime 0, so without a holder its window would be dropped and fetched again
 * (with cursors that may have expired). A retention is a disabled observer on
 * exactly that list query: it never fetches, it only keeps the cached window
 * out of garbage collection until the opener mounts again and releases it, for
 * at most RETURN_RETENTION_MS. Every retention is released with the private
 * state (logout, a failed check, an account switch), before the cache is cleared.
 */

export const RETURN_RETENTION_MS = 10 * 60 * 1000;
const MAX_RETENTIONS = 4;

type ObserverOptions = ConstructorParameters<typeof InfiniteQueryObserver>[1];
type Retention = {release(): void};

const retentions = new Map<string, Retention>();

function releaseAllReturnRetentions(): void {
  for (const retention of [...retentions.values()]) retention.release();
  retentions.clear();
}

/**
 * Hold the opener's list query (built from the same options as the opener's,
 * so the namespace-bound key matches) while the viewer closes. Nothing is
 * retained for a namespace that is no longer current.
 */
export function retainForReturn(
  queryClient: QueryClient,
  options: {queryKey: QueryKey},
  namespace: string,
  returnKey: string,
): void {
  const key = storeKey(namespace, returnKey);
  retentions.get(key)?.release();
  if (!isPrivateStateCurrent(capturePrivateState(), namespace)) return;
  const observer = new InfiniteQueryObserver(queryClient, {
    ...(options as unknown as ObserverOptions),
    enabled: false,
  });
  const unsubscribe = observer.subscribe(() => undefined);
  let released = false;
  const retention: Retention = {
    release() {
      if (released) return;
      released = true;
      clearTimeout(timer);
      unsubscribe();
      if (retentions.get(key) === retention) retentions.delete(key);
    },
  };
  const timer = setTimeout(() => retention.release(), RETURN_RETENTION_MS);
  retentions.set(key, retention);
  while (retentions.size > MAX_RETENTIONS) {
    const oldest = retentions.values().next().value as Retention | undefined;
    if (oldest === undefined) break;
    oldest.release();
  }
}

/** Whether the opener at `returnKey` can take its window back from a retention. */
export function isReturnRetained(namespace: string, returnKey: string): boolean {
  return retentions.has(storeKey(namespace, returnKey));
}

/** Release the opener's retention once its own observer holds the window. */
export function releaseReturnRetention(namespace: string, returnKey: string): void {
  retentions.get(storeKey(namespace, returnKey))?.release();
}
