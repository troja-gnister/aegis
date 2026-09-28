import {useQueryClient} from "@tanstack/react-query";
import {useEffect, useId, useRef, useState} from "react";
import {ApiProblem} from "../../api/problem";
import {capturePrivateState, isPrivateStateCurrent} from "../auth/cache";
import {useAuthSession} from "../auth/session";
import {formatExactCount, formatInstant} from "./format";
import {indexStatusQueryOptions, requestRootScan} from "./queries";
import type {IndexStatus} from "./types";
import {useIndexStatus} from "./useIndexStatus";

export type IndexStatusPanelProps = {
  rootId: string;
  /** Derived only from the root's `root_admin` permission, never platform-admin status. */
  canRequestScan: boolean;
  rootEpoch?: number;
  onStatus?(status: IndexStatus): void;
};

type RescanState =
  | {phase: "idle"}
  | {phase: "pending"; requestId: string}
  | {phase: "succeeded"}
  | {phase: "retryable"; requestId: string; message: string}
  | {phase: "failed"; message: string};

const STATE_TEXT: Record<IndexStatus["state"], string> = {
  not_indexed: "Not indexed yet",
  queued: "Indexing queued",
  scanning: "Indexing in progress",
  ready: "Index ready",
  degraded: "Indexed with problems",
  unavailable: "Source unavailable",
};

function stateDescription(status: IndexStatus): string {
  switch (status.state) {
    case "not_indexed":
      return "This root has not been indexed yet. Entries appear after the first scan.";
    case "queued":
      return "A scan is waiting for the indexer.";
    case "scanning":
      return "Observed counts grow as the scan continues; no total is known yet.";
    case "ready":
      return "The last scan completed. Later source changes appear after the next scan.";
    case "degraded": {
      const count = formatExactCount(status.degradedDirectories);
      const noun = status.degradedDirectories === "1" ? "directory" : "directories";
      return `${count} ${noun} could not be indexed. Their last indexed metadata is kept.`;
    }
    case "unavailable":
      return "The source cannot be reached right now. Showing last indexed metadata.";
  }
}

/** RFC 4122 version-4 UUID from getRandomValues, which also works outside secure contexts. */
function newRequestId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function rescanFailure(error: unknown, requestId: string): RescanState {
  const status = error instanceof ApiProblem ? error.status : 0;
  if (status === 429) {
    const delay = error instanceof ApiProblem ? error.retryAfterSeconds : undefined;
    const wait = delay === undefined ? "Try again shortly." :
      `Try again in ${delay} ${delay === 1 ? "second" : "seconds"}.`;
    return {phase: "retryable", requestId, message: `Rescan requests are temporarily limited. ${wait}`};
  }
  if (status === 0 || status >= 500) {
    return {
      phase: "retryable",
      requestId,
      message: "The rescan request could not be confirmed. Retrying sends the same request.",
    };
  }
  if (status === 403) return {phase: "failed", message: "You are not allowed to request a rescan for this root."};
  if (status === 404) return {phase: "failed", message: "This root is no longer available."};
  if (status === 401) return {phase: "failed", message: "Your session has ended."};
  return {phase: "failed", message: "The rescan request was not accepted."};
}

export function IndexStatusPanel({rootId, canRequestScan, rootEpoch = 0, onStatus}: IndexStatusPanelProps) {
  const id = useId();
  const {session} = useAuthSession();
  const namespace = session.cacheNamespace;
  const queryClient = useQueryClient();
  const query = useIndexStatus(namespace, rootId, rootEpoch);
  const status = query.data;
  const [rescan, setRescan] = useState<RescanState>({phase: "idle"});
  const inFlightRef = useRef(false);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (status) onStatus?.(status);
  }, [onStatus, status]);

  const request = async () => {
    if (inFlightRef.current) return;
    const requestId = rescan.phase === "retryable" ? rescan.requestId : newRequestId();
    const authority = capturePrivateState();
    const owned = () => mountedRef.current && isPrivateStateCurrent(authority, namespace);
    inFlightRef.current = true;
    setRescan({phase: "pending", requestId});
    try {
      await requestRootScan(queryClient, namespace, rootId, requestId);
      if (!owned()) return;
      setRescan({phase: "succeeded"});
      void queryClient.invalidateQueries({
        queryKey: indexStatusQueryOptions(namespace, rootId, rootEpoch).queryKey,
        exact: true,
      });
    } catch (error) {
      // A completion for a replaced account or closed view must not touch current UI.
      if (!owned()) return;
      setRescan(rescanFailure(error, requestId));
    } finally {
      inFlightRef.current = false;
    }
  };

  const pending = rescan.phase === "pending";
  const rescanMessage = rescan.phase === "succeeded" ? "Rescan requested."
    : rescan.phase === "retryable" || rescan.phase === "failed" ? rescan.message
      : pending ? "Requesting rescan…" : "";

  return (
    <section className="index-status" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="index-status__title">Index status</h2>
      {status ? (
        <>
          <p
            className={`index-status__state index-status__state--${status.state}`}
            aria-live="polite"
          >
            {STATE_TEXT[status.state]}
          </p>
          <p className="index-status__detail">{stateDescription(status)}</p>
          <dl className="index-status__counters">
            <div><dt>Observed entries</dt><dd>{formatExactCount(status.observedEntries)}</dd></div>
            <div><dt>Completed directories</dt><dd>{formatExactCount(status.completedDirectories)}</dd></div>
            <div><dt>Directories with problems</dt><dd>{formatExactCount(status.degradedDirectories)}</dd></div>
            <div><dt>Status updated</dt><dd>{formatInstant(status.updatedAt) ?? "Not yet"}</dd></div>
            <div><dt>Last completed scan</dt><dd>{formatInstant(status.lastCompletedAt) ?? "None yet"}</dd></div>
          </dl>
          {query.isError ? (
            <p className="index-status__detail">Status could not be refreshed. Retrying automatically.</p>
          ) : null}
        </>
      ) : query.isError ? (
        <p className="notice notice--error" role="alert">Index status could not be loaded.</p>
      ) : (
        <p role="status">Loading index status…</p>
      )}
      {canRequestScan ? (
        <div className="index-status__actions">
          <button
            className="file-page-control interactive"
            type="button"
            aria-disabled={pending || undefined}
            aria-busy={pending || undefined}
            onClick={() => void request()}
          >
            {rescan.phase === "retryable" ? "Retry rescan request" : "Request rescan"}
          </button>
          <p className="index-status__message" role="status">{rescanMessage}</p>
        </div>
      ) : null}
    </section>
  );
}
