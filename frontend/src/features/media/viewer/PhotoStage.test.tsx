import {notifyManager, QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {act, fireEvent, render, screen} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi} from "vitest";
import {ApiProblem} from "../../../api/problem";
import {installPointerEvents} from "../../../test/pointer-events";
import {server} from "../../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../../auth/cache";
import {AuthSessionContext, beginSignOut} from "../../auth/session";
import type {SessionResponse} from "../../auth/types";
import * as mediaApi from "../api";
import {useEntryMediaDetails} from "../hooks";
import {derivativeUrlFor, mediaDetailsBody, originalUrlFor, thumbnailUrlFor} from "../test-fixtures";
import {validatedMediaDetails} from "../validate";
import {PhotoStage} from "./PhotoStage";
import {MAX_POLL_ATTEMPTS, pollDelay, retryAfterDelay} from "./useViewingDerivative";

const ENTRY_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_ID = "44444444-4444-4444-8444-444444444444";
const SESSION: SessionResponse = {
  user: {id: "1797b1eb-c642-4f66-99a4-a878347b4e49", username: "alice"},
  cacheNamespace: "photo-stage-namespace",
};
const clients = new Set<QueryClient>();

let restorePointerEvents = () => {};

function Harness({entryId, onSwipe = () => undefined}: {entryId: string; onSwipe?(direction: string): void}) {
  const {query, details} = useEntryMediaDetails(entryId);
  return (
    <PhotoStage
      key={entryId}
      entryId={entryId}
      details={details}
      detailsFailed={query.isError && details === undefined}
      refetchDetails={query.refetch}
      thumbnailUrl={thumbnailUrlFor(entryId)}
      dimensions={null}
      alt="Synthetic photo"
      onSwipe={onSwipe}
    />
  );
}

async function renderStage(entryId = ENTRY_ID, onSwipe?: (direction: string) => void) {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
  const tree = (id: string) => (
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <Harness entryId={id} onSwipe={onSwipe} />
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
  const view = render(tree(entryId));
  return {queryClient, ...view, show: (id: string) => view.rerender(tree(id))};
}

/** A photo whose preview is in `state` (and ready with a URL when "ready"). */
function photoDetails(entryId: string, state: string, overrides: Record<string, unknown> = {}) {
  return validatedMediaDetails(mediaDetailsBody(entryId, {
    derivatives: {
      thumbnail: {state: "ready", url: derivativeUrlFor(entryId, "thumbnail")},
      preview: {state, url: state === "ready" ? derivativeUrlFor(entryId, "preview") : null},
    },
    ...overrides,
  }), entryId);
}

async function advance(milliseconds: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
}

/** Let chained answers (details, then the request, then a refetch) settle without moving the clock. */
async function settle() {
  for (let step = 0; step < 4; step += 1) await advance(0);
}

function callsFor(spy: {mock: {calls: unknown[][]}}, entryId: string): number {
  return spy.mock.calls.filter(([id]) => id === entryId).length;
}

beforeAll(() => {
  restorePointerEvents = installPointerEvents();
});

afterAll(() => restorePointerEvents());

beforeEach(() => {
  // Query notifications flush in microtasks, so fake timers never hold them back.
  notifyManager.setScheduler(queueMicrotask);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(320);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(480);
});

afterEach(async () => {
  notifyManager.setScheduler((callback) => setTimeout(callback, 0));
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete (document as unknown as Record<string, unknown>).visibilityState;
  await Promise.all([...clients].map((queryClient) => purgePrivateBrowserState(queryClient)));
  clients.clear();
});

describe("preview requests and polling", () => {
  it("backs off from 1 second to 10 seconds", () => {
    expect([0, 1, 2, 3, 4, 5, 20].map(pollDelay)).toEqual([1000, 2000, 4000, 8000, 10000, 10000, 10000]);
  });

  it("requests a missing preview once, shows the thumbnail, and polls with backoff until it is ready", async () => {
    vi.useFakeTimers();
    let state = "missing";
    const fetchMedia = vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, state));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative")
      .mockImplementation(async () => {
        state = "pending";
        return {state: "pending", kind: "preview"};
      });
    await renderStage();
    await settle();

    expect(fetchMedia).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledExactlyOnceWith(ENTRY_ID, "preview", expect.any(AbortSignal));
    expect(screen.getByRole("img", {name: "Synthetic photo"})).toHaveAttribute("src", derivativeUrlFor(ENTRY_ID, "thumbnail"));
    expect(screen.getByRole("status")).toHaveTextContent("Preparing a larger preview…");

    let calls = 1;
    for (const delay of [1000, 2000, 4000, 8000, 10000, 10000]) {
      await advance(delay - 1);
      expect(fetchMedia).toHaveBeenCalledTimes(calls);
      await advance(1);
      calls += 1;
      expect(fetchMedia).toHaveBeenCalledTimes(calls);
    }
    expect(request).toHaveBeenCalledTimes(1);

    state = "ready";
    await advance(10000);
    expect(screen.getByRole("img", {name: "Synthetic photo"})).toHaveAttribute("src", derivativeUrlFor(ENTRY_ID, "preview"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    const settled = fetchMedia.mock.calls.length;
    await advance(60_000);
    expect(fetchMedia).toHaveBeenCalledTimes(settled);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("polls a pending preview without requesting it again", async () => {
    vi.useFakeTimers();
    const fetchMedia = vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, "pending"));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative");
    await renderStage();
    await settle();
    await advance(1000);
    await advance(2000);

    expect(fetchMedia).toHaveBeenCalledTimes(3);
    expect(request).not.toHaveBeenCalled();
  });

  it("stops polling when the viewer leaves the photo", async () => {
    vi.useFakeTimers();
    const fetchMedia = vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, "pending"));
    const {unmount} = await renderStage();
    await settle();
    await advance(1000);
    expect(fetchMedia).toHaveBeenCalledTimes(2);

    unmount();
    await advance(60_000);

    expect(fetchMedia).toHaveBeenCalledTimes(2);
  });

  it("cancels the previous photo's request and polls when the entry changes", async () => {
    vi.useFakeTimers();
    const fetchMedia = vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, "missing"));
    let firstSignal: AbortSignal | undefined;
    const request = vi.spyOn(mediaApi, "requestMediaDerivative")
      .mockImplementation((entryId, kind, signal) => {
        if (entryId === ENTRY_ID) {
          firstSignal = signal;
          return new Promise(() => undefined);
        }
        return Promise.resolve({state: "pending", kind});
      });
    const {show} = await renderStage();
    await settle();
    expect(callsFor(request, ENTRY_ID)).toBe(1);

    show(OTHER_ID);
    await settle();

    expect(firstSignal?.aborted).toBe(true);
    expect(callsFor(request, OTHER_ID)).toBe(1);
    const firstEntryFetches = callsFor(fetchMedia, ENTRY_ID);
    await advance(30_000);
    expect(callsFor(fetchMedia, ENTRY_ID)).toBe(firstEntryFetches);
    expect(callsFor(fetchMedia, OTHER_ID)).toBeGreaterThan(1);
  });

  it("says an unsupported file has no preview, keeps the thumbnail, offers Download and never retries", async () => {
    vi.useFakeTimers();
    const fetchMedia = vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, "missing"));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative").mockRejectedValue(new ApiProblem({
      type: "media_unsupported", title: "Media unsupported", status: 409,
    }));
    await renderStage();
    await settle();

    expect(screen.getByRole("status")).toHaveTextContent("Preview isn't available for this file.");
    expect(screen.getByRole("img", {name: "Synthetic photo"})).toHaveAttribute("src", derivativeUrlFor(ENTRY_ID, "thumbnail"));
    expect(screen.getByRole("link", {name: "Download original"}))
      .toHaveAttribute("href", originalUrlFor(ENTRY_ID, "attachment"));
    // The inline original of an unsupported (possibly quarantined) file is never shown.
    expect(screen.queryAllByRole("img").map((image) => image.getAttribute("src")))
      .not.toContain(originalUrlFor(ENTRY_ID, "inline"));
    await advance(120_000);
    expect(fetchMedia).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("never shows the original of a quarantined photo", async () => {
    vi.useFakeTimers();
    vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, "quarantined", {permissions: {preview: true, export: false}}));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative");
    await renderStage();
    await settle();

    expect(screen.getByRole("status")).toHaveTextContent("Preview isn't available for this file.");
    expect(screen.queryAllByRole("img").map((image) => image.getAttribute("src")))
      .not.toContain(originalUrlFor(ENTRY_ID, "inline"));
    expect(screen.queryByRole("link", {name: "Download original"})).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });

  it("waits out a request backoff before allowing one manual retry", async () => {
    vi.useFakeTimers();
    let state = "missing";
    vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async (entryId) => photoDetails(entryId, state));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative")
      .mockRejectedValueOnce(new ApiProblem({
        type: "media_request_backoff", title: "Media request failed recently", status: 409, retryAfterSeconds: 30,
      }))
      .mockImplementation(async () => {
        state = "pending";
        return {state: "pending", kind: "preview"};
      });
    await renderStage();
    await settle();

    expect(screen.getByRole("status")).toHaveTextContent("Couldn't prepare this file; try again later.");
    const retry = screen.getByRole("button", {name: "Try again"});
    expect(retry).toBeDisabled();
    await advance(29_999);
    expect(screen.getByRole("button", {name: "Try again"})).toBeDisabled();
    expect(request).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(screen.getByRole("button", {name: "Try again"})).toBeEnabled();
    expect(request).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", {name: "Try again"}));
    await settle();

    expect(request).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("status")).toHaveTextContent("Preparing a larger preview…");
  });

  it("retries once after a 429, at Retry-After, then waits for the user", async () => {
    vi.useFakeTimers();
    vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async (entryId) => photoDetails(entryId, "missing"));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative").mockRejectedValue(new ApiProblem({
      type: "media_request_limit", title: "Too many media requests", status: 429, retryAfterSeconds: 3,
    }));
    await renderStage();
    await settle();

    expect(screen.getByRole("status")).toHaveTextContent("Busy, retrying…");
    await advance(2999);
    expect(request).toHaveBeenCalledTimes(1);
    await advance(1);
    await settle();
    expect(request).toHaveBeenCalledTimes(2);

    // A second 429 is not retried automatically.
    expect(screen.getByRole("status")).toHaveTextContent("Couldn't prepare this file; try again later.");
    await advance(300_000);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("clamps Retry-After to two minutes", async () => {
    vi.useFakeTimers();
    vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async (entryId) => photoDetails(entryId, "missing"));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative")
      .mockRejectedValueOnce(new ApiProblem({type: "request_failed", title: "Request failed", status: 429, retryAfterSeconds: 3600}))
      .mockResolvedValue({state: "pending", kind: "preview"});
    await renderStage();
    await settle();

    await advance(119_999);
    expect(request).toHaveBeenCalledTimes(1);
    await advance(1);
    await settle();
    expect(request).toHaveBeenCalledTimes(2);
    expect([0, 1, 30, 120, 3600, undefined].map(retryAfterDelay)).toEqual([1000, 1000, 30_000, 120_000, 120_000, 5000]);
  });

  it("cancels a pending 429 retry when the viewer leaves the photo", async () => {
    vi.useFakeTimers();
    vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async (entryId) => photoDetails(entryId, "missing"));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative").mockRejectedValue(new ApiProblem({
      type: "media_request_limit", title: "Too many media requests", status: 429, retryAfterSeconds: 3,
    }));
    const {unmount} = await renderStage();
    await settle();
    expect(screen.getByRole("status")).toHaveTextContent("Busy, retrying…");

    unmount();
    await advance(60_000);

    expect(request).toHaveBeenCalledTimes(1);
  });

  it("pauses polling while the page is hidden and resumes when it is visible", async () => {
    vi.useFakeTimers();
    const fetchMedia = vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, "pending"));
    await renderStage();
    await settle();
    expect(fetchMedia).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "visibilityState", {configurable: true, value: "hidden"});
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await advance(60_000);
    expect(fetchMedia).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "visibilityState", {configurable: true, value: "visible"});
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await advance(1000);
    expect(fetchMedia).toHaveBeenCalledTimes(2);
  });

  it("requests the preview again when it falls back from pending to missing", async () => {
    vi.useFakeTimers();
    let state = "missing";
    vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async (entryId) => photoDetails(entryId, state));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative").mockImplementation(async () => {
      state = "pending";
      return {state: "pending", kind: "preview"};
    });
    await renderStage();
    await settle();
    expect(request).toHaveBeenCalledTimes(1);
    await advance(1000);

    state = "missing";
    await advance(2000);
    await settle();

    expect(request).toHaveBeenCalledTimes(2);
  });

  it("stops polling after about ten minutes and offers a retry", async () => {
    vi.useFakeTimers();
    const fetchMedia = vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, "pending", {original: {inlineUrl: null}}));
    await renderStage();
    await settle();
    for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt += 1) await advance(pollDelay(attempt));
    expect(fetchMedia).toHaveBeenCalledTimes(MAX_POLL_ATTEMPTS + 1);

    expect(screen.getByRole("status")).toHaveTextContent("taking longer than expected");
    await advance(60_000);
    expect(fetchMedia).toHaveBeenCalledTimes(MAX_POLL_ATTEMPTS + 1);

    fireEvent.click(screen.getByRole("button", {name: "Try the preview again"}));
    await settle();
    await advance(1000);
    expect(fetchMedia).toHaveBeenCalledTimes(MAX_POLL_ATTEMPTS + 2);
  });

  it("falls back to the inline original when the preview failed", async () => {
    vi.useFakeTimers();
    vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async (entryId) => photoDetails(entryId, "failed"));
    await renderStage();
    await settle();

    expect(screen.getByRole("img", {name: "Synthetic photo"})).toHaveAttribute("src", originalUrlFor(ENTRY_ID, "inline"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("offers a retry for a failed preview and requests it again", async () => {
    vi.useFakeTimers();
    let state = "failed";
    vi.spyOn(mediaApi, "fetchEntryMedia")
      .mockImplementation(async (entryId) => photoDetails(entryId, state, {original: {inlineUrl: null}}));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative").mockImplementation(async () => {
      state = "pending";
      return {state: "pending", kind: "preview"};
    });
    await renderStage();
    await settle();
    expect(request).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", {name: "Try the preview again"}));
    await settle();

    expect(request).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent("Preparing a larger preview…");
  });
});

describe("late answers after logout", () => {
  it("discards a preview request that completes after logout and polls no further", async () => {
    let gets = 0;
    let release = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    let started = () => {};
    const requestStarted = new Promise<void>((resolve) => { started = resolve; });
    server.use(
      http.get(`/api/v1/entries/${ENTRY_ID}/media`, () => {
        gets += 1;
        return HttpResponse.json(mediaDetailsBody(ENTRY_ID, {derivatives: {
          thumbnail: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "thumbnail")},
          preview: {state: gets > 1 ? "ready" : "missing", url: gets > 1 ? derivativeUrlFor(ENTRY_ID, "preview") : null},
        }}));
      }),
      http.post(`/api/v1/entries/${ENTRY_ID}/media/requests`, async () => {
        started();
        await held;
        return HttpResponse.json({state: "ready", kind: "preview"}, {status: 202});
      }),
    );
    const {queryClient} = await renderStage();
    await requestStarted;

    await act(async () => {
      beginSignOut();
      await purgePrivateBrowserState(queryClient);
    });
    await act(async () => {
      release();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1100));
    });

    expect(gets).toBe(1);
    expect(screen.queryByRole("img", {name: "Synthetic photo"})?.getAttribute("src"))
      .not.toBe(derivativeUrlFor(ENTRY_ID, "preview"));
  });

  it("never shows a preview from a details answer that arrives after logout", async () => {
    let release = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    let started = () => {};
    const requestStarted = new Promise<void>((resolve) => { started = resolve; });
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}/media`, async () => {
      started();
      await held;
      return HttpResponse.json(mediaDetailsBody(ENTRY_ID, {derivatives: {
        thumbnail: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "thumbnail")},
        preview: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "preview")},
      }}));
    }));
    const {queryClient} = await renderStage();
    await requestStarted;

    await act(async () => {
      beginSignOut();
      await purgePrivateBrowserState(queryClient);
      release();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const sources = screen.queryAllByRole("img").map((image) => image.getAttribute("src"));
    expect(sources).not.toContain(derivativeUrlFor(ENTRY_ID, "preview"));
  });
});

describe("photo gestures", () => {
  function stage() {
    return screen.getByTestId("photo-stage");
  }

  function tap(x: number, y: number, pointerId = 1) {
    fireEvent.pointerDown(stage(), {pointerId, pointerType: "touch", clientX: x, clientY: y});
    fireEvent.pointerUp(stage(), {pointerId, pointerType: "touch", clientX: x, clientY: y});
  }

  beforeEach(() => {
    server.use(http.get(`/api/v1/entries/:entryId/media`, ({params}) =>
      HttpResponse.json(mediaDetailsBody(String(params.entryId), {derivatives: {
        thumbnail: {state: "ready", url: derivativeUrlFor(String(params.entryId), "thumbnail")},
        preview: {state: "ready", url: derivativeUrlFor(String(params.entryId), "preview")},
      }}))));
  });

  it.each([
    [300, 100, "next"],
    [100, 300, "previous"],
  ])("swipes from x=%d to x=%d to show the %s photo", async (from, to, direction) => {
    const onSwipe = vi.fn();
    await renderStage(ENTRY_ID, onSwipe);
    fireEvent.pointerDown(stage(), {pointerId: 1, pointerType: "touch", clientX: from, clientY: 200});
    fireEvent.pointerMove(stage(), {pointerId: 1, pointerType: "touch", clientX: (from + to) / 2, clientY: 202});
    fireEvent.pointerUp(stage(), {pointerId: 1, pointerType: "touch", clientX: to, clientY: 205});
    expect(onSwipe).toHaveBeenCalledExactlyOnceWith(direction);
  });

  it("ignores short and vertical drags", async () => {
    const onSwipe = vi.fn();
    await renderStage(ENTRY_ID, onSwipe);
    fireEvent.pointerDown(stage(), {pointerId: 1, pointerType: "touch", clientX: 200, clientY: 100});
    fireEvent.pointerUp(stage(), {pointerId: 1, pointerType: "touch", clientX: 190, clientY: 400});
    expect(onSwipe).not.toHaveBeenCalled();
  });

  it("zooms with a double tap, pans instead of swiping while zoomed, and zooms back out", async () => {
    const onSwipe = vi.fn();
    await renderStage(ENTRY_ID, onSwipe);

    tap(160, 240);
    tap(160, 240);
    expect(stage()).toHaveAttribute("data-scale", "2.5");

    fireEvent.pointerDown(stage(), {pointerId: 2, pointerType: "touch", clientX: 300, clientY: 240});
    fireEvent.pointerMove(stage(), {pointerId: 2, pointerType: "touch", clientX: 100, clientY: 240});
    fireEvent.pointerUp(stage(), {pointerId: 2, pointerType: "touch", clientX: 100, clientY: 240});
    expect(onSwipe).not.toHaveBeenCalled();

    tap(160, 240);
    tap(160, 240);
    expect(stage()).toHaveAttribute("data-scale", "1");
  });

  it("zooms with a pinch", async () => {
    await renderStage();
    fireEvent.pointerDown(stage(), {pointerId: 1, pointerType: "touch", clientX: 100, clientY: 240});
    fireEvent.pointerDown(stage(), {pointerId: 2, pointerType: "touch", clientX: 220, clientY: 240});
    fireEvent.pointerMove(stage(), {pointerId: 2, pointerType: "touch", clientX: 340, clientY: 240});
    expect(stage()).toHaveAttribute("data-scale", "2");
    fireEvent.pointerUp(stage(), {pointerId: 2, pointerType: "touch", clientX: 340, clientY: 240});
    fireEvent.pointerUp(stage(), {pointerId: 1, pointerType: "touch", clientX: 100, clientY: 240});
    expect(stage()).toHaveAttribute("data-scale", "2");
  });

  it("zooms and pans with the keyboard, and leaves unzoomed arrows to the viewer", async () => {
    await renderStage();
    const element = stage();

    // `fireEvent` returns false when the stage consumed the key (preventDefault).
    expect(fireEvent.keyDown(element, {key: "ArrowRight"})).toBe(true);

    expect(fireEvent.keyDown(element, {key: "+"})).toBe(false);
    expect(element).toHaveAttribute("data-scale", "1.5");
    expect(fireEvent.keyDown(element, {key: "ArrowRight"})).toBe(false);
    fireEvent.keyDown(element, {key: "0"});
    expect(element).toHaveAttribute("data-scale", "1");
    fireEvent.keyDown(element, {key: "-"});
    expect(element).toHaveAttribute("data-scale", "1");
    expect(element).toHaveAttribute("tabindex", "0");
    expect(element).toHaveAccessibleDescription(/Plus and minus zoom/);
  });

  it("zooms with a mouse double click", async () => {
    await renderStage();
    fireEvent.pointerDown(stage(), {pointerId: 1, pointerType: "mouse", button: 0, clientX: 160, clientY: 240});
    fireEvent.pointerUp(stage(), {pointerId: 1, pointerType: "mouse", button: 0, clientX: 160, clientY: 240});
    fireEvent.doubleClick(stage(), {clientX: 160, clientY: 240});
    expect(stage()).toHaveAttribute("data-scale", "2.5");
  });
});
