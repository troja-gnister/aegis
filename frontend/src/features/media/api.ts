import {apiRequest} from "../../api/http";
import {ApiProblem} from "../../api/problem";
import {invalidResponse, uuid} from "../../api/validate";
import {obtainCsrfToken} from "../auth/api";
import {assertAuthority, authorityBoundRequest} from "../auth/authority";
import {capturePrivateState, isPrivateStateCurrent} from "../auth/cache";
import {isSessionAccessOpen} from "../auth/session";
import type {MediaDetails, MediaRequestResult, RequestableDerivativeKind} from "./types";
import {validatedMediaDetails, validatedMediaRequestResult} from "./validate";

const MEDIA_RESPONSE_BYTES = 64 * 1024;
const requestableKinds = new Set<RequestableDerivativeKind>(["preview", "transcode"]);

/**
 * Load media metadata, derivative states and delivery URLs for one entry.
 * A 404 means the entry is not media (or is no longer available); a 401
 * surfaces as an ApiProblem for the query layer's sign-out handling.
 */
export async function fetchEntryMedia(entryId: string, signal: AbortSignal): Promise<MediaDetails> {
  uuid(entryId);
  const snapshot = capturePrivateState();
  assertAuthority(snapshot, signal);
  const response = await authorityBoundRequest(
    apiRequest<unknown>(`/api/v1/entries/${entryId}/media`, {
      signal, maxResponseBytes: MEDIA_RESPONSE_BYTES,
    }),
    snapshot,
    signal,
  );
  const result = validatedMediaDetails(response, entryId);
  assertAuthority(snapshot, signal);
  return result;
}

async function postMediaRequest(
  entryId: string,
  kind: RequestableDerivativeKind,
  csrfToken: string,
  signal: AbortSignal | undefined,
): Promise<unknown> {
  return apiRequest<unknown>(`/api/v1/entries/${entryId}/media/requests`, {
    method: "POST",
    body: {kind},
    csrfToken,
    signal,
    maxResponseBytes: MEDIA_RESPONSE_BYTES,
  });
}

/**
 * Ask for an on-demand derivative (`preview` for photos, `transcode` for
 * videos) under the initiating session. CSRF comes from the shared auth
 * store; one explicit CSRF rejection is retried with a replacement token. The
 * request is idempotent server-side while pending or ready. A 409 (the asset
 * is unsupported or quarantined) and a 401 surface as ApiProblems; a
 * completion after logout or an account switch is rejected.
 */
export async function requestMediaDerivative(
  entryId: string,
  kind: RequestableDerivativeKind,
  signal?: AbortSignal,
): Promise<MediaRequestResult> {
  uuid(entryId);
  if (!requestableKinds.has(kind)) invalidResponse();
  const snapshot = capturePrivateState();
  assertAuthority(snapshot, signal);
  const isCurrent = () => !signal?.aborted && isSessionAccessOpen() && isPrivateStateCurrent(snapshot);
  const response = await authorityBoundRequest((async () => {
    const token = await obtainCsrfToken(isCurrent);
    try {
      return await postMediaRequest(entryId, kind, token, signal);
    } catch (error) {
      if (!(error instanceof ApiProblem) || error.type !== "csrf_failed" || !isCurrent()) throw error;
      return postMediaRequest(entryId, kind, await obtainCsrfToken(isCurrent, token), signal);
    }
  })(), snapshot, signal);
  const result = validatedMediaRequestResult(response, kind);
  assertAuthority(snapshot, signal);
  return result;
}
