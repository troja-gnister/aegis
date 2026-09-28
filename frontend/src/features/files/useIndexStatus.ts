import {useQuery} from "@tanstack/react-query";
import {useEffect, useState} from "react";
import {ApiProblem} from "../../api/problem";
import {indexStatusQueryOptions} from "./queries";
import type {IndexStatus} from "./types";

export const ACTIVE_STATUS_INTERVAL = 3000;
export const IDLE_STATUS_INTERVAL = 30000;
export const MAX_STATUS_BACKOFF = 60000;

export function statusPollInterval(state: IndexStatus["state"], visible: boolean,
                                   online: boolean): number | false {
  if (!visible || !online) return false;
  return state === "queued" || state === "scanning" ? ACTIVE_STATUS_INTERVAL : IDLE_STATUS_INTERVAL;
}

/** Exponential failure delay, never faster than the state's interval and capped at 60 seconds. */
export function statusFailureDelay(failures: number, base: number): number {
  const exponential = ACTIVE_STATUS_INTERVAL * 2 ** Math.min(Math.max(failures, 0), 16);
  return Math.min(MAX_STATUS_BACKOFF, Math.max(base, exponential));
}

function currentlyVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

function currentlyOnline(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

function usePageActivity() {
  const [visible, setVisible] = useState(currentlyVisible);
  const [online, setOnline] = useState(currentlyOnline);
  useEffect(() => {
    const visibility = () => setVisible(currentlyVisible());
    const connectivity = () => setOnline(currentlyOnline());
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("online", connectivity);
    window.addEventListener("offline", connectivity);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("online", connectivity);
      window.removeEventListener("offline", connectivity);
    };
  }, []);
  return {visible, online};
}

function isTerminalFailure(error: unknown): boolean {
  return error instanceof ApiProblem && [401, 403, 404].includes(error.status);
}

/**
 * Bounded metadata status polling (not SSE): 3 seconds while queued/scanning,
 * 30 seconds otherwise, paused while hidden or offline, with failures backing
 * off up to 60 seconds. This hook must be the single poller for a root.
 */
export function useIndexStatus(namespace: string, rootId: string, rootEpoch: number) {
  const {visible, online} = usePageActivity();
  const query = useQuery({
    ...indexStatusQueryOptions(namespace, rootId, rootEpoch),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const {errorUpdateCount, dataUpdatedAt, errorUpdatedAt, isFetching, refetch} = query;
  // Error count observed at the last success; consecutive failures derive from it synchronously.
  const [successBaseline, setSuccessBaseline] = useState(0);
  const state = query.data?.state;
  const stopped = query.isError && isTerminalFailure(query.error);
  const failed = errorUpdatedAt > dataUpdatedAt;
  const failures = failed ? errorUpdateCount - successBaseline : 0;

  useEffect(() => {
    // Only a success that is the latest settlement moves the baseline.
    if (dataUpdatedAt > 0 && dataUpdatedAt >= errorUpdatedAt) setSuccessBaseline(errorUpdateCount);
  }, [dataUpdatedAt, errorUpdateCount, errorUpdatedAt]);

  useEffect(() => {
    if (isFetching || stopped) return;
    const base = statusPollInterval(state ?? "queued", visible, online);
    if (base === false) return;
    const delay = failed ? statusFailureDelay(failures, base) : base;
    const settledAt = Math.max(dataUpdatedAt, errorUpdatedAt);
    const wait = Math.max(0, delay - (Date.now() - settledAt));
    const timer = setTimeout(() => {
      void refetch();
    }, wait);
    return () => clearTimeout(timer);
  }, [dataUpdatedAt, errorUpdatedAt, failed, failures, isFetching, online, refetch, state, stopped, visible]);

  return query;
}
