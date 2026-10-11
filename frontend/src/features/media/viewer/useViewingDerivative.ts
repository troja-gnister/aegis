import {useCallback, useEffect, useRef, useState} from "react";
import {ApiProblem} from "../../../api/problem";
import {isSessionAccessOpen} from "../../auth/session";
import {useRequestMediaDerivative} from "../hooks";
import type {MediaDetails, RequestableDerivativeKind} from "../types";

export const POLL_INITIAL_MS = 1000;
export const POLL_MAX_MS = 10_000;

/** Backoff between derivative polls: 1 s, doubling, capped at 10 s. */
export function pollDelay(attempt: number): number {
  return Math.min(POLL_MAX_MS, POLL_INITIAL_MS * 2 ** Math.max(0, attempt));
}

/** About ten minutes of polling (1 + 2 + 4 + 8 s, then 58 × 10 s) before offering Retry. */
export const MAX_POLL_ATTEMPTS = 62;

function currentlyVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

/** Whether the page is visible; polling pauses while it is hidden, like index status polling. */
function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(currentlyVisible);
  useEffect(() => {
    const update = () => setVisible(currentlyVisible());
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

export type DerivativePhase =
  | "idle"
  | "loading"
  | "error"
  | "ready"
  | "preparing"
  | "stalled"
  | "failed"
  | "unsupported"
  | "unavailable";

type RequestStatus = "idle" | "pending" | "done" | "failed" | "blocked";

export type ViewingDerivative = {
  phase: DerivativePhase;
  url: string | null;
  /** Ask for the derivative again (after a failure) and restart polling. */
  retry(): void;
};

export type ViewingDerivativeOptions = {
  entryId: string;
  kind: RequestableDerivativeKind;
  details: MediaDetails | undefined;
  detailsFailed: boolean;
  refetchDetails(): Promise<unknown>;
  enabled: boolean;
};

/**
 * A viewing derivative (photo preview or video transcode) for the current
 * entry. When it is missing, it is requested once (the request is idempotent
 * server-side); while it is pending, media details are polled with backoff
 * from 1 s to 10 s. Requests and polls stop on unmount, which the viewer
 * forces on every entry change. Completions after logout are discarded by the
 * session-bound request and query layers.
 */
export function useViewingDerivative({
  entryId,
  kind,
  details,
  detailsFailed,
  refetchDetails,
  enabled,
}: ViewingDerivativeOptions): ViewingDerivative {
  const requestDerivative = useRequestMediaDerivative();
  const [requestStatus, setRequestStatus] = useState<RequestStatus>("idle");
  const [retries, setRetries] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const requestedRef = useRef(false);
  const sawPendingRef = useRef(false);
  const refetchRef = useRef(refetchDetails);
  const visible = useDocumentVisible();

  useEffect(() => {
    refetchRef.current = refetchDetails;
  });

  const previewAllowed = details?.permissions.preview ?? false;
  const derivative = details ? details.derivatives[kind] ?? {state: "missing" as const, url: null} : undefined;
  const state = derivative?.state;

  let phase: DerivativePhase;
  if (!enabled) phase = "idle";
  else if (!details || !derivative) phase = detailsFailed ? "error" : "loading";
  else if (!previewAllowed) phase = "unavailable";
  else if (derivative.state === "ready") phase = derivative.url === null ? "unavailable" : "ready";
  else if (derivative.state === "unsupported" || derivative.state === "quarantined" || requestStatus === "blocked") {
    phase = "unsupported";
  } else if (requestStatus === "failed") phase = "failed";
  else if (derivative.state === "failed" && !(retries > 0 && requestStatus !== "done")) phase = "failed";
  else phase = attempt >= MAX_POLL_ATTEMPTS ? "stalled" : "preparing";

  const shouldRequest = enabled && previewAllowed &&
    (state === "missing" || (state === "failed" && retries > 0));

  useEffect(() => {
    // A derivative that falls back from pending to missing (evicted or invalidated) is asked for again.
    if (state === "pending") {
      sawPendingRef.current = true;
    } else if (state === "missing" && sawPendingRef.current) {
      sawPendingRef.current = false;
      requestedRef.current = false;
      setRequestStatus("idle");
    }
  }, [state]);

  useEffect(() => {
    if (!shouldRequest || requestedRef.current) return;
    requestedRef.current = true;
    let settled = false;
    const controller = new AbortController();
    setRequestStatus("pending");
    requestDerivative(entryId, kind, controller.signal).then(
      (result) => {
        settled = true;
        if (controller.signal.aborted) return;
        setRequestStatus("done");
        setAttempt(0);
        // A ready answer is shown at once, and a retry re-reads the failed state it replaced;
        // a first pending answer is polled from here, starting after 1 s.
        if (result.state === "ready" || retries > 0) void refetchRef.current();
      },
      (error: unknown) => {
        settled = true;
        if (controller.signal.aborted || !isSessionAccessOpen()) return;
        setRequestStatus(error instanceof ApiProblem && error.status === 409 ? "blocked" : "failed");
      },
    );
    return () => {
      // An interrupted request (the derivative changed state, or React's development
      // re-run) no longer holds polling back, and may be sent again if still needed.
      if (!settled) {
        requestedRef.current = false;
        setRequestStatus("idle");
      }
      controller.abort();
    };
  }, [entryId, kind, requestDerivative, retries, shouldRequest]);

  const polling = phase === "preparing" && requestStatus !== "pending" && visible;

  useEffect(() => {
    if (!polling) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      void refetchRef.current().finally(() => {
        if (!cancelled) setAttempt((current) => current + 1);
      });
    }, pollDelay(attempt));
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [attempt, polling]);

  const retry = useCallback(() => {
    requestedRef.current = false;
    setRequestStatus("idle");
    setRetries((count) => count + 1);
    setAttempt(0);
  }, []);

  return {phase, url: phase === "ready" ? derivative?.url ?? null : null, retry};
}
