import {
  infiniteQueryOptions,
  queryOptions,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query";
import {ApiProblem, genericApiProblem} from "../../api/problem";
import {purgePrivateBrowserState} from "../auth/cache";
import {beginSignOut, completeSignOut} from "../auth/session";
import {fetchDirectory, fetchEntry, fetchIndexStatus} from "./api";
import {clearFileNavigationRoot} from "./navigation";
import type {BrowseInput, DirectoryPage} from "./types";

async function handleCatalogFailure(
  error: unknown,
  queryClient: QueryClient,
  namespace: string,
  rootId?: string,
): Promise<never> {
  if (error instanceof ApiProblem && error.status === 401) {
    const generation = beginSignOut();
    const cancellation = queryClient.cancelQueries({queryKey: ["files"]});
    const cleanup = purgePrivateBrowserState(queryClient);
    await Promise.all([cancellation, cleanup]);
    completeSignOut(generation, true);
  } else if (error instanceof ApiProblem && error.status === 404 && rootId !== undefined) {
    clearFileNavigationRoot(namespace, rootId);
    await queryClient.invalidateQueries({queryKey: ["roots", namespace]});
  }
  throw error;
}

function rejectsRetainedDuplicate(
  queryClient: QueryClient,
  queryKey: readonly unknown[],
  pageParam: string | null,
  page: DirectoryPage,
): void {
  const retained = queryClient.getQueryData<InfiniteData<DirectoryPage>>(queryKey);
  if (!retained) return;
  if (retained.pageParams.some((retainedParam) => Object.is(retainedParam, pageParam))) return;
  const ids = new Set(retained.pages.flatMap((item) => item.entries.map((entry) => entry.id)));
  if (page.entries.some((entry) => ids.has(entry.id))) throw genericApiProblem(502);
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
      try {
        const page = await fetchDirectory({...stableInput, cursor: pageParam}, signal);
        rejectsRetainedDuplicate(client, queryKey, pageParam, page);
        return page;
      } catch (error) {
        return handleCatalogFailure(error, client, input.namespace, input.rootId);
      }
    },
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    getPreviousPageParam: (page) => page.previousCursor ?? undefined,
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
      try {
        return await fetchEntry(id, signal);
      } catch (error) {
        return handleCatalogFailure(error, client, namespace);
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
      try {
        return await fetchIndexStatus(rootId, signal);
      } catch (error) {
        return handleCatalogFailure(error, client, namespace, rootId);
      }
    },
    gcTime: 0,
    staleTime: 5000,
    retry: false,
  });
}
