import {
  infiniteQueryOptions,
  queryOptions,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query";
import {ApiProblem, genericApiProblem} from "../../api/problem";
import {
  capturePrivateState,
  isPrivateStateCurrent,
  purgePrivateBrowserState,
  type PrivateStateSnapshot,
} from "../auth/cache";
import {beginSignOut, completeSignOut, isSessionAccessOpen} from "../auth/session";
import {fetchDirectory, fetchEntry, fetchIndexStatus} from "./api";
import {clearFileNavigationRoot} from "./navigation";
import type {BrowseInput, DirectoryPage} from "./types";

async function handleCatalogFailure(
  error: unknown,
  queryClient: QueryClient,
  namespace: string,
  authority: PrivateStateSnapshot,
  signal: AbortSignal,
  rootId?: string,
): Promise<never> {
  assertQueryAuthority(authority, namespace, signal);
  if (error instanceof ApiProblem && error.status === 401) {
    const generation = beginSignOut();
    const cancellation = queryClient.cancelQueries({queryKey: ["files"]});
    const cleanup = purgePrivateBrowserState(queryClient).then(() => true, () => false);
    const [cleanupSucceeded] = await Promise.all([cleanup, cancellation]);
    completeSignOut(generation, true, cleanupSucceeded);
  } else if (error instanceof ApiProblem && error.status === 404 && rootId !== undefined) {
    clearFileNavigationRoot(namespace, rootId);
    await queryClient.invalidateQueries({queryKey: ["roots", namespace]});
  }
  throw error;
}

function assertQueryAuthority(
  authority: PrivateStateSnapshot,
  namespace: string,
  signal: AbortSignal,
): void {
  if (signal.aborted || !isSessionAccessOpen() || !isPrivateStateCurrent(authority, namespace)) {
    throw genericApiProblem();
  }
}

function captureQueryAuthority(namespace: string, signal: AbortSignal): PrivateStateSnapshot {
  const authority = capturePrivateState();
  assertQueryAuthority(authority, namespace, signal);
  return authority;
}

function deduplicateWindow(
  data: InfiniteData<DirectoryPage>,
): InfiniteData<DirectoryPage> {
  const ids = new Set<string>();
  let changed = false;
  const pages = data.pages.map((page) => {
    const entries = page.entries.filter((entry) => {
      if (ids.has(entry.id)) return false;
      ids.add(entry.id);
      return true;
    });
    if (entries.length === page.entries.length) return page;
    changed = true;
    return {...page, entries};
  });
  return changed ? {...data, pages} : data;
}

export function directoryQueryOptions(input: BrowseInput) {
  const stableInput = {...input, cursor: undefined};
  const queryKey = [
    "files", input.namespace, input.rootId, input.rootEpoch, input.parentId,
    input.filters, input.sort, input.order,
  ] as const;
  return infiniteQueryOptions({
    queryKey,
    initialPageParam: input.cursor ?? null,
    queryFn: async ({pageParam, signal, client}) => {
      const authority = captureQueryAuthority(input.namespace, signal);
      try {
        const page = await fetchDirectory({...stableInput, cursor: pageParam}, signal);
        assertQueryAuthority(authority, input.namespace, signal);
        return page;
      } catch (error) {
        return handleCatalogFailure(
          error, client, input.namespace, authority, signal, input.rootId,
        );
      }
    },
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    getPreviousPageParam: (page) => page.previousCursor ?? undefined,
    select: deduplicateWindow,
    maxPages: 5,
    gcTime: 0,
    staleTime: 5000,
    retry: false,
  });
}

export function entryQueryOptions(namespace: string, id: string) {
  return queryOptions({
    queryKey: ["files", namespace, "entry", id],
    queryFn: async ({signal, client}) => {
      const authority = captureQueryAuthority(namespace, signal);
      try {
        const entry = await fetchEntry(id, signal);
        assertQueryAuthority(authority, namespace, signal);
        return entry;
      } catch (error) {
        return handleCatalogFailure(error, client, namespace, authority, signal);
      }
    },
    gcTime: 0,
    staleTime: 5000,
    retry: false,
  });
}

export function indexStatusQueryOptions(namespace: string, rootId: string, rootEpoch: number) {
  return queryOptions({
    queryKey: ["files", namespace, "index-status", rootId, rootEpoch],
    queryFn: async ({signal, client}) => {
      const authority = captureQueryAuthority(namespace, signal);
      try {
        const status = await fetchIndexStatus(rootId, signal);
        assertQueryAuthority(authority, namespace, signal);
        return status;
      } catch (error) {
        return handleCatalogFailure(error, client, namespace, authority, signal, rootId);
      }
    },
    gcTime: 0,
    staleTime: 5000,
    retry: false,
  });
}
