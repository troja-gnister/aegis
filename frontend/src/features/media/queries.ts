import {queryOptions, type QueryClient} from "@tanstack/react-query";
import {assertQueryAuthority, captureQueryAuthority} from "../auth/authority";
import {handleCatalogFailure} from "../files/queries";
import {fetchEntryMedia, requestMediaDerivative} from "./api";
import type {MediaRequestResult, RequestableDerivativeKind} from "./types";

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
