import {renderHook} from "@testing-library/react";
import {afterEach, describe, expect, it, vi} from "vitest";
import {derivativeUrlFor} from "../test-fixtures";
import {isSameOriginMediaUrl, useNeighborImagePrefetch} from "./prefetch";

const ENTRY_ID = "33333333-3333-4333-8333-333333333333";

afterEach(() => vi.unstubAllGlobals());

describe("neighbor prefetch", () => {
  it.each([
    [derivativeUrlFor(ENTRY_ID, "preview"), true],
    ["/media/v1/originals/x?d=inline", true],
    ["//evil.test/media/v1/derivatives/x", false],
    ["https://evil.test/media/v1/derivatives/x", false],
    ["/api/v1/entries/x/media", false],
    ["media/v1/derivatives/x", false],
    ["javascript:alert(1)", false],
  ])("accepts %s: %s", (url, expected) => {
    expect(isSameOriginMediaUrl(url)).toBe(expected);
  });

  it("loads only same-origin media URLs and abandons them when the neighbors change", () => {
    const images: {src: string}[] = [];
    class RecordingImage {
      decoding = "auto";
      src = "";
      constructor() {
        images.push(this);
      }
    }
    vi.stubGlobal("Image", RecordingImage);
    const preview = derivativeUrlFor(ENTRY_ID, "preview");
    const {rerender, unmount} = renderHook(({urls}: {urls: (string | null)[]}) => useNeighborImagePrefetch(urls), {
      initialProps: {urls: [preview, "//evil.test/x.webp", "/api/v1/roots", null]},
    });

    expect(images.map((image) => image.src)).toEqual([preview]);

    rerender({urls: [null, null]});
    expect(images[0]!.src).toBe("");
    expect(images).toHaveLength(1);

    unmount();
  });
});
