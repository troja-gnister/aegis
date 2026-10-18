import {describe, expect, it} from "vitest";
import {classifyExtension, mediaKindOf, PHOTO_EXTENSIONS, VIDEO_EXTENSIONS} from "./classify";
import {entryMediaFixture} from "./test-fixtures";

const ENTRY_ID = "33333333-3333-4333-8333-333333333333";

describe("media classification", () => {
  it("matches the design's photo and video allowlists exactly", () => {
    expect([...PHOTO_EXTENSIONS]).toEqual([
      "jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "avif", "bmp", "tif", "tiff",
    ]);
    expect([...VIDEO_EXTENSIONS]).toEqual([
      "mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "3g2", "mts", "m2ts",
      "wmv", "asf", "flv", "f4v", "mpg", "mpeg", "vob", "ogv", "divx",
    ]);
  });

  it.each(PHOTO_EXTENSIONS.map((extension) => [extension]))("classifies .%s as a photo", (extension) => {
    expect(classifyExtension(extension)).toBe("photo");
  });

  it.each(VIDEO_EXTENSIONS.map((extension) => [extension]))("classifies .%s as a video", (extension) => {
    expect(classifyExtension(extension)).toBe("video");
  });

  it("accepts a leading dot and any case", () => {
    expect(classifyExtension(".JPG")).toBe("photo");
    expect(classifyExtension("HeIc")).toBe("photo");
    expect(classifyExtension(".M2TS")).toBe("video");
    expect(classifyExtension(".WMV")).toBe("video");
    expect(classifyExtension("3G2")).toBe("video");
  });

  it.each([
    ["image"], ["video"], ["pdf"], ["txt"], ["raw"], ["cr2"], ["mp3"], ["jpgx"], ["xjpg"], [""], ["."],
    // TypeScript sources and audio-only containers are not videos.
    ["ts"], ["tsx"], ["wma"], ["ogg"], ["m4a"],
  ])("never classifies %j, including MIME family words, as media", (extension) => {
    expect(classifyExtension(extension)).toBeNull();
  });

  it("treats a missing extension as non-media", () => {
    expect(classifyExtension(null)).toBeNull();
    expect(classifyExtension(undefined)).toBeNull();
  });

  it("classifies only regular files by extension", () => {
    expect(mediaKindOf({kind: "file", typeHint: "jpg", media: null})).toBe("photo");
    expect(mediaKindOf({kind: "file", typeHint: "mov", media: null})).toBe("video");
    expect(mediaKindOf({kind: "file", typeHint: "wmv", media: null})).toBe("video");
    expect(mediaKindOf({kind: "directory", typeHint: "jpg", media: null})).toBeNull();
    expect(mediaKindOf({kind: "symlink", typeHint: "mp4", media: null})).toBeNull();
    expect(mediaKindOf({kind: "special", typeHint: "png", media: null})).toBeNull();
    expect(mediaKindOf({kind: "file", typeHint: null, media: null})).toBeNull();
  });

  it("prefers the server's media kind over the extension", () => {
    expect(mediaKindOf({
      kind: "file", typeHint: "jpg", media: entryMediaFixture(ENTRY_ID, {kind: "video"}),
    })).toBe("video");
  });
});
