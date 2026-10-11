import {notifyManager, QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {act, fireEvent, render, screen} from "@testing-library/react";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {activateCacheNamespace, purgePrivateBrowserState} from "../../auth/cache";
import {AuthSessionContext} from "../../auth/session";
import type {SessionResponse} from "../../auth/types";
import * as mediaApi from "../api";
import {useEntryMediaDetails} from "../hooks";
import {derivativeUrlFor, mediaDetailsBody, originalUrlFor, thumbnailUrlFor} from "../test-fixtures";
import {validatedMediaDetails} from "../validate";
import type {CanPlayType} from "./video-source";
import {VideoStage} from "./VideoStage";

const ENTRY_ID = "55555555-5555-4555-8555-555555555555";
const PLAYBACK_TYPE = "video/mp4; codecs=\"avc1.640028, mp4a.40.2\"";
const SESSION: SessionResponse = {
  user: {id: "1797b1eb-c642-4f66-99a4-a878347b4e49", username: "alice"},
  cacheNamespace: "video-stage-namespace",
};
const clients = new Set<QueryClient>();

function Harness({canPlayType}: {canPlayType: CanPlayType}) {
  const {query, details} = useEntryMediaDetails(ENTRY_ID);
  return (
    <VideoStage
      entryId={ENTRY_ID}
      details={details}
      detailsFailed={query.isError && details === undefined}
      refetchDetails={query.refetch}
      thumbnailUrl={thumbnailUrlFor(ENTRY_ID)}
      title="Synthetic video"
      canPlayType={canPlayType}
    />
  );
}

async function renderStage(canPlayType: CanPlayType) {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, SESSION.cacheNamespace);
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <Harness canPlayType={canPlayType} />
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
}

/** A video whose transcode is in `state`; other overrides replace the fixture's fields. */
function videoDetails(state: string, overrides: Record<string, unknown> = {}) {
  return validatedMediaDetails(mediaDetailsBody(ENTRY_ID, {
    kind: "video",
    derivatives: {
      thumbnail: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "thumbnail")},
      poster: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "poster")},
      transcode: {state, url: state === "ready" ? derivativeUrlFor(ENTRY_ID, "transcode") : null},
    },
    ...overrides,
  }), ENTRY_ID);
}

async function advance(milliseconds: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
}

async function settle() {
  for (let step = 0; step < 4; step += 1) await advance(0);
}

function video(): HTMLVideoElement | null {
  return document.querySelector("video");
}

beforeEach(() => {
  // Query notifications flush in microtasks, so fake timers never hold them back.
  notifyManager.setScheduler(queueMicrotask);
  vi.useFakeTimers();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
});

afterEach(async () => {
  notifyManager.setScheduler((callback) => setTimeout(callback, 0));
  vi.useRealTimers();
  vi.restoreAllMocks();
  await Promise.all([...clients].map((queryClient) => purgePrivateBrowserState(queryClient)));
  clients.clear();
});

describe("VideoStage", () => {
  it("plays the inline original with native controls when the browser can play its codecs", async () => {
    vi.spyOn(mediaApi, "fetchEntryMedia").mockResolvedValue(videoDetails("missing"));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative");
    const canPlayType = vi.fn(() => "probably");
    await renderStage(canPlayType);
    await settle();

    expect(canPlayType).toHaveBeenCalledWith(PLAYBACK_TYPE);
    const element = screen.getByLabelText("Synthetic video");
    expect(element.tagName).toBe("VIDEO");
    expect(element).toHaveAttribute("src", originalUrlFor(ENTRY_ID, "inline"));
    expect(element).toHaveAttribute("poster", derivativeUrlFor(ENTRY_ID, "poster"));
    expect(element).toHaveAttribute("controls");
    expect(request).not.toHaveBeenCalled();
    expect(screen.queryByText("Preparing playback…")).not.toBeInTheDocument();
  });

  it("requests a transcode once when the browser cannot play the original, then plays it", async () => {
    let state = "missing";
    const fetchMedia = vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async () => videoDetails(state));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative").mockImplementation(async () => {
      state = "pending";
      return {state: "pending", kind: "transcode"};
    });
    await renderStage(() => "");
    await settle();

    expect(request).toHaveBeenCalledExactlyOnceWith(ENTRY_ID, "transcode", expect.any(AbortSignal));
    expect(screen.getByRole("status")).toHaveTextContent("Preparing playback…");
    expect(video()).toBeNull();

    await advance(1000);
    expect(fetchMedia).toHaveBeenCalledTimes(2);
    expect(video()).toBeNull();

    state = "ready";
    await advance(2000);

    expect(fetchMedia).toHaveBeenCalledTimes(3);
    expect(video()).toHaveAttribute("src", derivativeUrlFor(ENTRY_ID, "transcode"));
    expect(video()).toHaveAttribute("poster", derivativeUrlFor(ENTRY_ID, "poster"));
    expect(screen.queryByText("Preparing playback…")).not.toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("falls back to a transcode when the original fails to play", async () => {
    let state = "missing";
    vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async () => videoDetails(state));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative").mockImplementation(async () => {
      state = "pending";
      return {state: "pending", kind: "transcode"};
    });
    await renderStage(() => "maybe");
    await settle();
    expect(video()).toHaveAttribute("src", originalUrlFor(ENTRY_ID, "inline"));

    fireEvent.error(video()!);
    await settle();

    expect(request).toHaveBeenCalledExactlyOnceWith(ENTRY_ID, "transcode", expect.any(AbortSignal));
    expect(screen.getByRole("status")).toHaveTextContent("Preparing playback…");
  });

  it("shows a retry when playback fails and reloads the video", async () => {
    vi.spyOn(mediaApi, "fetchEntryMedia").mockResolvedValue(videoDetails("ready"));
    await renderStage(() => "");
    await settle();
    const first = video();
    expect(first).toHaveAttribute("src", derivativeUrlFor(ENTRY_ID, "transcode"));

    fireEvent.error(first!);
    expect(screen.getByRole("status")).toHaveTextContent("This video could not be played.");
    expect(video()).toBeNull();

    fireEvent.click(screen.getByRole("button", {name: "Retry playback"}));

    expect(video()).toHaveAttribute("src", derivativeUrlFor(ENTRY_ID, "transcode"));
    expect(video()).not.toBe(first);
  });

  it("offers a retry when the transcode could not be prepared", async () => {
    let state = "failed";
    vi.spyOn(mediaApi, "fetchEntryMedia").mockImplementation(async () => videoDetails(state));
    const request = vi.spyOn(mediaApi, "requestMediaDerivative").mockImplementation(async () => {
      state = "pending";
      return {state: "pending", kind: "transcode"};
    });
    await renderStage(() => "");
    await settle();
    expect(screen.getByRole("status")).toHaveTextContent("Playback could not be prepared.");
    expect(request).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", {name: "Retry playback"}));
    await settle();

    expect(request).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent("Preparing playback…");
  });

  it("says so when the format cannot be played at all", async () => {
    vi.spyOn(mediaApi, "fetchEntryMedia").mockResolvedValue(videoDetails("unsupported", {original: {inlineUrl: null}}));
    await renderStage(() => "");
    await settle();
    expect(screen.getByRole("status")).toHaveTextContent("This video cannot be played in the browser.");
    expect(video()).toBeNull();
  });

  it("plays nothing without PREVIEW", async () => {
    vi.spyOn(mediaApi, "fetchEntryMedia").mockResolvedValue(
      videoDetails("missing", {permissions: {preview: false, export: true}, derivatives: {
        thumbnail: {state: "ready", url: null},
        poster: {state: "ready", url: null},
        transcode: {state: "missing", url: null},
      }}),
    );
    const request = vi.spyOn(mediaApi, "requestMediaDerivative");
    await renderStage(() => "probably");
    await settle();
    expect(screen.getByRole("status")).toHaveTextContent("Your access to this file does not include playing it.");
    expect(video()).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });
});
