import {afterEach, describe, expect, it, vi} from "vitest";
import {validatedMediaDetails} from "../validate";
import {mediaDetailsBody, originalUrlFor} from "../test-fixtures";
import {browserCanPlayType, chooseVideoSource} from "./video-source";

const ENTRY_ID = "33333333-3333-4333-8333-333333333333";
const PLAYBACK_TYPE = "video/mp4; codecs=\"avc1.640028, mp4a.40.2\"";

function video(overrides: Record<string, unknown> = {}) {
  return validatedMediaDetails(mediaDetailsBody(ENTRY_ID, {kind: "video", ...overrides}), ENTRY_ID);
}

afterEach(() => vi.restoreAllMocks());

describe("video playability", () => {
  it.each(["probably", "maybe"])("plays the inline original when the browser answers %j", (answer) => {
    const canPlayType = vi.fn(() => answer);
    expect(chooseVideoSource(video(), canPlayType)).toEqual({source: "original", url: originalUrlFor(ENTRY_ID, "inline")});
    expect(canPlayType).toHaveBeenCalledWith(PLAYBACK_TYPE);
  });

  it("asks for a transcode when the browser cannot play the probed codecs", () => {
    expect(chooseVideoSource(video(), () => "")).toEqual({source: "transcode"});
  });

  it("asks for a transcode when no codec string was probed or no inline original exists", () => {
    const canPlayType = vi.fn(() => "probably");
    expect(chooseVideoSource(video({original: {playbackType: null}}), canPlayType)).toEqual({source: "transcode"});
    expect(chooseVideoSource(video({original: {inlineUrl: null}}), canPlayType)).toEqual({source: "transcode"});
    expect(canPlayType).not.toHaveBeenCalled();
  });

  it("plays nothing without PREVIEW", () => {
    expect(chooseVideoSource(video({permissions: {preview: false, export: true}}), () => "probably"))
      .toEqual({source: "none"});
  });

  it("asks a video element by default", () => {
    const canPlayType = vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockReturnValue("maybe");
    expect(browserCanPlayType(PLAYBACK_TYPE)).toBe("maybe");
    expect(canPlayType).toHaveBeenCalledWith(PLAYBACK_TYPE);
    expect(chooseVideoSource(video()).source).toBe("original");
    canPlayType.mockReturnValue("");
    expect(chooseVideoSource(video()).source).toBe("transcode");
  });
});
