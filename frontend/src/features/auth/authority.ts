import {genericApiProblem} from "../../api/problem";
import {capturePrivateState, isPrivateStateCurrent, type PrivateStateSnapshot} from "./cache";
import {isSessionAccessOpen} from "./session";

/*
 * Authority checks for private requests: a request, its response and its
 * failure handling belong to the session and cache namespace that started it.
 * Any of them completing after logout, an account switch or an abort is
 * refused with a generic problem and never published.
 */

/** Rejects a failure that completes after the initiating session or namespace was replaced. */
export async function authorityBoundRequest<T>(
  request: Promise<T>,
  snapshot: PrivateStateSnapshot,
  signal?: AbortSignal,
  namespace = snapshot.namespace,
): Promise<T> {
  try {
    return await request;
  } catch (error) {
    assertAuthority(snapshot, signal, namespace);
    throw error;
  }
}

export function assertAuthority(
  snapshot: PrivateStateSnapshot,
  signal?: AbortSignal,
  namespace = snapshot.namespace,
): void {
  if (signal?.aborted || !isSessionAccessOpen() || !isPrivateStateCurrent(snapshot, namespace)) {
    throw genericApiProblem();
  }
}

export function assertQueryAuthority(
  authority: PrivateStateSnapshot,
  namespace: string,
  signal: AbortSignal,
): void {
  if (signal.aborted || !isSessionAccessOpen() || !isPrivateStateCurrent(authority, namespace)) {
    throw genericApiProblem();
  }
}

export function captureQueryAuthority(namespace: string, signal: AbortSignal): PrivateStateSnapshot {
  const authority = capturePrivateState();
  assertQueryAuthority(authority, namespace, signal);
  return authority;
}
