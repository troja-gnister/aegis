import {useQuery, useQueryClient, type UseQueryOptions} from "@tanstack/react-query";
import {useCallback, useEffect} from "react";
import {useAuthSession} from "../auth/session";
import {mediaDetailsQueryOptions, mediaQueryKeys, requestMediaDerivativeForSession} from "./queries";
import type {MediaDetails, MediaRequestResult, RequestableDerivativeKind} from "./types";

type MediaDetailsQueryOptions = UseQueryOptions<
  MediaDetails, Error, MediaDetails, ReturnType<typeof mediaQueryKeys.details>
>;

export type EntryMediaDetailsOptions = {
  enabled?: boolean;
  /**
   * Polling for a viewer waiting on a derivative: milliseconds, false, or a
   * function of the query (for backoff, and to return false once the
   * derivative is ready or the query failed). Defaults to false.
   */
  refetchInterval?: MediaDetailsQueryOptions["refetchInterval"];
};

/**
 * Media details for one entry under the current session's namespace. The
 * request is cancelled when the owner unmounts or switches entries, and a
 * previous entry's details are never returned for a new entry ID.
 */
export function useEntryMediaDetails(entryId: string, options: EntryMediaDetailsOptions = {}) {
  const {session} = useAuthSession();
  const queryClient = useQueryClient();
  const queryOptions = mediaDetailsQueryOptions(session.cacheNamespace, entryId);
  const query = useQuery({
    ...queryOptions,
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval ?? false,
    refetchOnWindowFocus: false,
  });
  const keyText = JSON.stringify(queryOptions.queryKey);

  useEffect(() => {
    const key = JSON.parse(keyText) as unknown[];
    return () => {
      void queryClient.cancelQueries({queryKey: key, exact: true});
    };
  }, [keyText, queryClient]);

  const details = query.data?.entryId === entryId ? query.data : undefined;
  return {query, details};
}

/** A session-bound `requestMediaDerivative` for viewers (preview or transcode). */
export function useRequestMediaDerivative(): (
  entryId: string,
  kind: RequestableDerivativeKind,
  signal?: AbortSignal,
) => Promise<MediaRequestResult> {
  const {session} = useAuthSession();
  const queryClient = useQueryClient();
  const namespace = session.cacheNamespace;
  return useCallback(
    (entryId, kind, signal) => requestMediaDerivativeForSession(queryClient, namespace, entryId, kind, signal),
    [namespace, queryClient],
  );
}
