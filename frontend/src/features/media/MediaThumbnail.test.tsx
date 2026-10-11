import {QueryClient} from "@tanstack/react-query";
import {fireEvent, render, screen} from "@testing-library/react";
import {afterEach, describe, expect, it, vi} from "vitest";
import {purgePrivateBrowserState} from "../auth/cache";
import {MediaThumbnail} from "./MediaThumbnail";
import {entryMediaFixture, thumbnailUrlFor} from "./test-fixtures";
import type {EntryMedia} from "./types";

const ENTRY_ID = "33333333-3333-4333-8333-333333333333";

function photo(media: Partial<EntryMedia> | null = {}) {
  return {
    kind: "file",
    typeHint: "jpg",
    media: media === null ? null : entryMediaFixture(ENTRY_ID, media),
  };
}

function boxOf(element: HTMLElement): HTMLElement {
  return element.closest(".media-thumbnail") as HTMLElement;
}

afterEach(async () => {
  vi.restoreAllMocks();
  // Forgets remembered thumbnail failures between tests, as logout does.
  await purgePrivateBrowserState(new QueryClient());
});

describe("MediaThumbnail", () => {
  it("renders a lazy, async, decorative image with fixed dimensions for a ready thumbnail", () => {
    const {container} = render(<MediaThumbnail entry={photo()} />);
    const image = container.querySelector("img")!;

    expect(image).toHaveAttribute("src", thumbnailUrlFor(ENTRY_ID));
    expect(image).toHaveAttribute("loading", "lazy");
    expect(image).toHaveAttribute("decoding", "async");
    expect(image).toHaveAttribute("alt", "");
    expect(image).toHaveAttribute("width", "44");
    expect(image).toHaveAttribute("height", "44");
    const box = image.parentElement!;
    expect(box).toHaveAttribute("data-state", "ready");
    expect(box.style.getPropertyValue("--media-thumbnail-size")).toBe("44px");
    expect(screen.queryByRole("img", {name: /thumbnail/})).not.toBeInTheDocument();
  });

  it("uses the requested box size for both the box and the image attributes", () => {
    const {container} = render(<MediaThumbnail entry={photo()} size={120} />);
    const image = container.querySelector("img")!;
    expect(image).toHaveAttribute("width", "120");
    expect(image).toHaveAttribute("height", "120");
    expect(image.parentElement!.style.getPropertyValue("--media-thumbnail-size")).toBe("120px");
  });

  it.each([
    ["pending", {thumbnail: "pending", thumbnailUrl: null}, "Photo, thumbnail is being prepared"],
    ["failed", {thumbnail: "failed", thumbnailUrl: null}, "Photo, thumbnail could not be created"],
    ["failed", {thumbnail: "quarantined", thumbnailUrl: null}, "Photo, thumbnail could not be created"],
    ["unsupported", {thumbnail: "unsupported", thumbnailUrl: null}, "Photo, thumbnails are not supported for this format"],
    ["unavailable", {thumbnail: "ready", thumbnailUrl: null}, "Photo, thumbnail is not available to you"],
  ] as const)("renders a %s icon with a distinct accessible name and no image", (state, media, name) => {
    const {container} = render(<MediaThumbnail entry={photo(media)} />);
    expect(boxOf(screen.getByRole("img", {name}))).toHaveAttribute("data-state", state);
    expect(container.querySelector("img")).toBeNull();
  });

  it("gives every non-ready state a different accessible name and a different badge glyph", () => {
    const entries = [
      photo({thumbnail: "pending", thumbnailUrl: null}),
      photo({thumbnail: "failed", thumbnailUrl: null}),
      photo({thumbnail: "unsupported", thumbnailUrl: null}),
      photo({thumbnail: "ready", thumbnailUrl: null}),
      photo(null),
    ];
    const seen = entries.map((entry) => {
      const view = render(<MediaThumbnail entry={entry} />);
      const name = view.getByRole("img").getAttribute("aria-label");
      const badge = view.container.querySelector(".media-thumbnail__badge")?.textContent;
      view.unmount();
      return {name, badge};
    });
    expect(new Set(seen.map((item) => item.name)).size).toBe(entries.length);
    expect(seen.every((item) => item.badge)).toBe(true);
    expect(new Set(seen.map((item) => item.badge)).size).toBe(entries.length);
  });

  it("names the kind of a video and of a media file the catalog has not described yet", () => {
    render(
      <>
        <MediaThumbnail entry={{kind: "file", typeHint: "mp4", media: entryMediaFixture(ENTRY_ID, {
          kind: "video", thumbnail: "pending", thumbnailUrl: null,
        })}} />
        <MediaThumbnail entry={photo(null)} />
      </>,
    );
    expect(screen.getByRole("img", {name: "Video, thumbnail is being prepared"})).toBeVisible();
    expect(boxOf(screen.getByRole("img", {name: "Photo"}))).toHaveAttribute("data-state", "unknown");
  });

  it.each([
    [{thumbnail: "pending" as const, thumbnailUrl: null}, "Photo, thumbnail is being prepared"],
    [{}, "Photo thumbnail"],
  ])("renders the state label as a text node for an enclosing description", (media, label) => {
    render(<MediaThumbnail entry={photo(media)} descriptionId="row-description" />);
    const description = document.getElementById("row-description")!;
    expect(description).toHaveTextContent(label);
    expect(description).toHaveClass("media-thumbnail__description");
  });

  it("falls back to the icon once when the image fails, without retrying the same URL", () => {
    const entry = photo();
    const {container, rerender} = render(<MediaThumbnail entry={entry} />);
    fireEvent.error(container.querySelector("img")!);

    expect(container.querySelector("img")).toBeNull();
    expect(boxOf(screen.getByRole("img", {name: "Photo, thumbnail could not be loaded"}))).toHaveAttribute(
      "data-state", "load-failed",
    );
    rerender(<MediaThumbnail entry={{...entry, media: {...entry.media!}}} />);
    expect(container.querySelector("img")).toBeNull();

    const renewed = thumbnailUrlFor(ENTRY_ID, 5);
    rerender(<MediaThumbnail entry={photo({thumbnailUrl: renewed})} />);
    expect(container.querySelector("img")).toHaveAttribute("src", renewed);
  });

  it("does not request a failed URL again after a remount until private state is purged", async () => {
    const first = render(<MediaThumbnail entry={photo()} />);
    fireEvent.error(first.container.querySelector("img")!);
    first.unmount();

    const remounted = render(<MediaThumbnail entry={photo()} />);
    expect(remounted.container.querySelector("img")).toBeNull();
    remounted.unmount();

    await purgePrivateBrowserState(new QueryClient());
    const afterPurge = render(<MediaThumbnail entry={photo()} />);
    expect(afterPurge.container.querySelector("img")).toHaveAttribute("src", thumbnailUrlFor(ENTRY_ID));
  });

  it("renders nothing for non-media entries", () => {
    const {container} = render(
      <>
        <MediaThumbnail entry={{kind: "file", typeHint: "pdf", media: null}} />
        <MediaThumbnail entry={{kind: "directory", typeHint: "jpg", media: null}} />
      </>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("never creates object URLs", () => {
    const createObjectUrl = vi.spyOn(URL, "createObjectURL");
    const {container} = render(<MediaThumbnail entry={photo()} />);
    expect(container.querySelector("img")!.getAttribute("src")).not.toMatch(/^blob:/);
    expect(createObjectUrl).not.toHaveBeenCalled();
  });
});
