import {onlineManager, QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {afterEach, describe, expect, it, vi} from "vitest";
import {genericApiProblem} from "../../api/problem";
import {server} from "../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {AuthSessionContext, isSessionAccessOpen} from "../auth/session";
import type {SessionResponse} from "../auth/types";
import * as fileApi from "./api";
import {IndexStatusPanel} from "./IndexStatusPanel";
import {FIXTURE_ROOT_ID} from "./test-fixtures";
import type {IndexStatus} from "./types";
import {statusFailureDelay, statusPollInterval} from "./useIndexStatus";

const SESSION: SessionResponse = {
  user: {id: "1797b1eb-c642-4f66-99a4-a878347b4e49", username: "alice"},
  cacheNamespace: "index-status-namespace",
};
const SCAN_ID = "55555555-5555-4555-8555-555555555555";
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const clients = new Set<QueryClient>();

function status(state: IndexStatus["state"], overrides: Partial<IndexStatus> = {}): IndexStatus {
  return {
    state,
    generation: "3",
    observedEntries: "9007199254740993",
    completedDirectories: "41",
    degradedDirectories: state === "degraded" ? "2" : "0",
    updatedAt: "2026-09-21T12:34:56.123456+00:00",
    lastCompletedAt: state === "not_indexed" ? null : "2026-09-21T11:00:00+00:00",
    ...overrides,
  };
}

async function renderStatus(canRequestScan = false, onStatus?: (value: IndexStatus) => void) {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
  render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <IndexStatusPanel rootId={FIXTURE_ROOT_ID} canRequestScan={canRequestScan} onStatus={onStatus} />
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
  return queryClient;
}

function statusHandler(value: IndexStatus | (() => IndexStatus)) {
  let calls = 0;
  server.use(http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/index-status`, () => {
    calls += 1;
    return HttpResponse.json(typeof value === "function" ? value() : value);
  }));
  return () => calls;
}

async function advance(milliseconds: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
}

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete (document as unknown as Record<string, unknown>).visibilityState;
  delete (navigator as unknown as Record<string, unknown>).onLine;
  onlineManager.setOnline(true);
  await Promise.all([...clients].map((client) => purgePrivateBrowserState(client)));
  clients.clear();
});

describe("status polling policy", () => {
  it("polls actively, idles, pauses hidden/offline and caps failure backoff", () => {
    expect(statusPollInterval("queued", true, true)).toBe(3000);
    expect(statusPollInterval("scanning", true, true)).toBe(3000);
    for (const state of ["not_indexed", "ready", "degraded", "unavailable"] as const) {
      expect(statusPollInterval(state, true, true)).toBe(30000);
    }
    expect(statusPollInterval("scanning", false, true)).toBe(false);
    expect(statusPollInterval("scanning", true, false)).toBe(false);
    expect([1, 2, 3, 4, 5, 6, 40].map((failures) => statusFailureDelay(failures, 3000)))
      .toEqual([6000, 12000, 24000, 48000, 60000, 60000, 60000]);
    expect(statusFailureDelay(1, 30000)).toBe(30000);
    expect(statusFailureDelay(2, 30000)).toBe(30000);
    expect(statusFailureDelay(5, 30000)).toBe(60000);
  });
});

describe("IndexStatusPanel", () => {
  it.each([
    ["not_indexed", "Not indexed yet", /has not been indexed/i],
    ["queued", "Indexing queued", /waiting for the indexer/i],
    ["scanning", "Indexing in progress", /counts grow as the scan continues/i],
    ["ready", "Index ready", /last scan completed/i],
    ["degraded", "Indexed with problems", /2 directories could not be indexed/i],
    ["unavailable", "Source unavailable", /last indexed metadata/i],
  ] as const)("labels the %s state with observed counters and no percentage", async (state, label, detail) => {
    statusHandler(status(state));
    await renderStatus();
    expect(await screen.findByText(label)).toBeVisible();
    const panel = screen.getByRole("region", {name: "Index status"});
    expect(panel).toHaveTextContent(detail);
    expect(panel).toHaveTextContent("9,007,199,254,740,993");
    expect(panel).toHaveTextContent("41");
    expect(panel).toHaveTextContent(/updated/i);
    expect(panel).not.toHaveTextContent("%");
  });

  it("distinguishes a failed status request from every index state", async () => {
    server.use(http.get(`/api/v1/roots/${FIXTURE_ROOT_ID}/index-status`, () =>
      HttpResponse.json({type: "catalog_unavailable", title: "Catalog unavailable"}, {status: 503}),
    ));
    await renderStatus();
    expect(await screen.findByText("Index status could not be loaded.")).toBeVisible();
    expect(screen.queryByText(/Not indexed yet|Index ready/)).not.toBeInTheDocument();
  });

  it("reports each status to the page owner", async () => {
    statusHandler(status("scanning"));
    const onStatus = vi.fn();
    await renderStatus(false, onStatus);
    await waitFor(() => expect(onStatus).toHaveBeenCalledWith(status("scanning")));
  });

  it("polls every 3 seconds while indexing and every 30 seconds when idle", async () => {
    vi.useFakeTimers();
    let current = status("scanning");
    const fetchStatus = vi.spyOn(fileApi, "fetchIndexStatus").mockImplementation(async () => current);
    await renderStatus();
    await advance(0);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    await advance(2999);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    current = status("ready");
    await advance(1);
    expect(fetchStatus).toHaveBeenCalledTimes(2);
    await advance(29_999);
    expect(fetchStatus).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(fetchStatus).toHaveBeenCalledTimes(3);
  });

  it("pauses polling while hidden and resumes when visible again", async () => {
    vi.useFakeTimers();
    const fetchStatus = vi.spyOn(fileApi, "fetchIndexStatus").mockResolvedValue(status("scanning"));
    await renderStatus();
    await advance(0);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, "visibilityState", {configurable: true, value: "hidden"});
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await advance(120_000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, "visibilityState", {configurable: true, value: "visible"});
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await advance(0);
    expect(fetchStatus).toHaveBeenCalledTimes(2);
  });

  it("pauses polling while offline and resumes when back online", async () => {
    vi.useFakeTimers();
    const fetchStatus = vi.spyOn(fileApi, "fetchIndexStatus").mockResolvedValue(status("queued"));
    await renderStatus();
    await advance(0);
    Object.defineProperty(navigator, "onLine", {configurable: true, value: false});
    await act(async () => { window.dispatchEvent(new Event("offline")); });
    await advance(120_000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    Object.defineProperty(navigator, "onLine", {configurable: true, value: true});
    await act(async () => { window.dispatchEvent(new Event("online")); });
    await advance(0);
    expect(fetchStatus).toHaveBeenCalledTimes(2);
  });

  it("backs off failed status requests up to 60 seconds and resets after success", async () => {
    vi.useFakeTimers();
    let fail = true;
    const fetchStatus = vi.spyOn(fileApi, "fetchIndexStatus").mockImplementation(async () => {
      if (fail) throw genericApiProblem(503);
      return status("scanning");
    });
    await renderStatus();
    await advance(0);
    let calls = 1;
    expect(fetchStatus).toHaveBeenCalledTimes(calls);
    for (const delay of [6000, 12000, 24000, 48000, 60000, 60000]) {
      await advance(delay - 1);
      expect(fetchStatus).toHaveBeenCalledTimes(calls);
      if (delay === 60000 && calls === 6) fail = false;
      await advance(1);
      calls += 1;
      expect(fetchStatus).toHaveBeenCalledTimes(calls);
    }
    await advance(2999);
    expect(fetchStatus).toHaveBeenCalledTimes(calls);
    await advance(1);
    expect(fetchStatus).toHaveBeenCalledTimes(calls + 1);
  });

  it("does not show a rescan action without the root_admin capability", async () => {
    statusHandler(status("ready"));
    await renderStatus(false);
    await screen.findByText("Index ready");
    expect(screen.queryByRole("button", {name: /rescan/i})).not.toBeInTheDocument();
  });

  it("requests a rescan with a new UUID request ID and the existing CSRF state", async () => {
    const statusCalls = statusHandler(status("ready"));
    const headers: Headers[] = [];
    let csrfCalls = 0;
    server.use(
      http.get("/api/v1/auth/csrf", () => {
        csrfCalls += 1;
        return HttpResponse.json({csrfToken: "csrf-account-a"});
      }),
      http.post(`/api/v1/roots/${FIXTURE_ROOT_ID}/scans`, ({request}) => {
        headers.push(request.headers);
        return HttpResponse.json({scanId: SCAN_ID}, {status: 202});
      }),
    );
    await renderStatus(true);
    await screen.findByText("Index ready");
    const before = statusCalls();
    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    expect(await screen.findByText("Rescan requested.")).toBeVisible();
    expect(headers[0]!.get("X-Request-ID")).toMatch(UUID_V4);
    expect(headers[0]!.get("X-CSRFToken")).toBe("csrf-account-a");
    await waitFor(() => expect(statusCalls()).toBeGreaterThan(before));

    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    await waitFor(() => expect(headers).toHaveLength(2));
    expect(headers[1]!.get("X-Request-ID")).toMatch(UUID_V4);
    expect(headers[1]!.get("X-Request-ID")).not.toBe(headers[0]!.get("X-Request-ID"));
    expect(csrfCalls).toBe(1);
  });

  it("retains the same request ID across a network retry", async () => {
    statusHandler(status("ready"));
    const requestIds: string[] = [];
    server.use(http.post(`/api/v1/roots/${FIXTURE_ROOT_ID}/scans`, ({request}) => {
      requestIds.push(request.headers.get("X-Request-ID") ?? "");
      return requestIds.length === 1
        ? HttpResponse.error()
        : HttpResponse.json({scanId: SCAN_ID}, {status: 202});
    }));
    await renderStatus(true);
    await screen.findByText("Index ready");
    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    expect(await screen.findByText(/could not be confirmed/i)).toBeVisible();
    fireEvent.click(screen.getByRole("button", {name: "Retry rescan request"}));
    expect(await screen.findByText("Rescan requested.")).toBeVisible();
    expect(requestIds).toHaveLength(2);
    expect(requestIds[1]).toBe(requestIds[0]);
  });

  it("explains a 429 with its bounded retry delay and keeps the same request for retry", async () => {
    statusHandler(status("ready"));
    const requestIds: string[] = [];
    server.use(http.post(`/api/v1/roots/${FIXTURE_ROOT_ID}/scans`, ({request}) => {
      requestIds.push(request.headers.get("X-Request-ID") ?? "");
      return requestIds.length === 1
        ? HttpResponse.json(
          {type: "scan_request_denied", title: "Scan request denied"},
          {status: 429, headers: {"Retry-After": "17"}},
        )
        : HttpResponse.json({scanId: SCAN_ID}, {status: 202});
    }));
    await renderStatus(true);
    await screen.findByText("Index ready");
    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    expect(await screen.findByText("Rescan requests are temporarily limited. Try again in 17 seconds."))
      .toBeVisible();
    fireEvent.click(screen.getByRole("button", {name: "Retry rescan request"}));
    expect(await screen.findByText("Rescan requested.")).toBeVisible();
    expect(requestIds[1]).toBe(requestIds[0]);
  });

  it("refreshes a rejected CSRF token once and keeps the request ID", async () => {
    statusHandler(status("ready"));
    const sent: Array<[string | null, string | null]> = [];
    let csrfCalls = 0;
    server.use(
      http.get("/api/v1/auth/csrf", () => {
        csrfCalls += 1;
        return HttpResponse.json({csrfToken: `csrf-${csrfCalls}`});
      }),
      http.post(`/api/v1/roots/${FIXTURE_ROOT_ID}/scans`, ({request}) => {
        sent.push([request.headers.get("X-CSRFToken"), request.headers.get("X-Request-ID")]);
        return sent.length === 1
          ? HttpResponse.json({type: "csrf_failed", title: "Request verification failed"}, {status: 403})
          : HttpResponse.json({scanId: SCAN_ID}, {status: 202});
      }),
    );
    await renderStatus(true);
    await screen.findByText("Index ready");
    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    expect(await screen.findByText("Rescan requested.")).toBeVisible();
    expect(sent.map(([token]) => token)).toEqual(["csrf-1", "csrf-2"]);
    expect(sent[1]![1]).toBe(sent[0]![1]);
  });

  it("explains a denied rescan and starts a fresh request afterwards", async () => {
    statusHandler(status("ready"));
    const requestIds: string[] = [];
    server.use(http.post(`/api/v1/roots/${FIXTURE_ROOT_ID}/scans`, ({request}) => {
      requestIds.push(request.headers.get("X-Request-ID") ?? "");
      return HttpResponse.json({type: "scan_request_denied", title: "Scan request denied"}, {status: 403});
    }));
    await renderStatus(true);
    await screen.findByText("Index ready");
    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    expect(await screen.findByText(/not allowed to request a rescan/i)).toBeVisible();
    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    await waitFor(() => expect(requestIds).toHaveLength(2));
    expect(requestIds[1]).not.toBe(requestIds[0]);
  });

  it("closes private access and purges state when a rescan returns 401", async () => {
    statusHandler(status("ready"));
    server.use(http.post(`/api/v1/roots/${FIXTURE_ROOT_ID}/scans`, () =>
      HttpResponse.json({type: "authentication_required", title: "Authentication required"}, {status: 401}),
    ));
    const queryClient = await renderStatus(true);
    queryClient.setQueryData(["private", SESSION.cacheNamespace], "secret");
    await screen.findByText("Index ready");
    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    await waitFor(() => expect(isSessionAccessOpen()).toBe(false));
    expect(queryClient.getQueryData(["private", SESSION.cacheNamespace])).toBeUndefined();
  });

  it("drops a rescan whose CSRF acquisition completes after the account changed", async () => {
    statusHandler(status("ready"));
    let releaseCsrf = () => {};
    const csrfGate = new Promise<void>((resolve) => { releaseCsrf = resolve; });
    let posts = 0;
    server.use(
      http.get("/api/v1/auth/csrf", async () => {
        await csrfGate;
        return HttpResponse.json({csrfToken: "csrf-account-a"});
      }),
      http.post(`/api/v1/roots/${FIXTURE_ROOT_ID}/scans`, () => {
        posts += 1;
        return HttpResponse.json({scanId: SCAN_ID}, {status: 202});
      }),
    );
    const queryClient = await renderStatus(true);
    await screen.findByText("Index ready");
    fireEvent.click(screen.getByRole("button", {name: "Request rescan"}));
    await act(async () => {
      await activateCacheNamespace(queryClient, "account-b-namespace");
    });
    releaseCsrf();
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(posts).toBe(0);
    expect(screen.queryByText("Rescan requested.")).not.toBeInTheDocument();
    expect(screen.queryByText(/could not be confirmed/i)).not.toBeInTheDocument();
  });
});
