import {fireEvent, render, screen} from "@testing-library/react";
import {describe, expect, it, vi} from "vitest";
import {entryMediaFixture, thumbnailUrlFor} from "../media/test-fixtures";
import {FileRow} from "./FileRow";
import {fixtureEntry} from "./test-fixtures";
import type {EntrySummary} from "./types";

function photoEntry(media: Partial<NonNullable<EntrySummary["media"]>> = {}): EntrySummary {
  const entry = fixtureEntry(7);
  return {...entry, typeHint: "jpg", media: entryMediaFixture(entry.id, media)};
}

describe("FileRow", () => {
  it("shows a ready photo thumbnail inside the row without changing the row's accessible name", () => {
    const entry = photoEntry();
    const {container} = render(<FileRow entry={entry} onOpen={() => undefined} />);

    const row = screen.getByRole("button", {name: `Select file ${entry.displayName}`});
    const image = row.querySelector("img")!;
    expect(image).toHaveAttribute("src", thumbnailUrlFor(entry.id));
    expect(image).toHaveAttribute("loading", "lazy");
    expect(image).toHaveAttribute("decoding", "async");
    expect(image.closest(".file-row__icon")).not.toBeNull();
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(row).toHaveAccessibleDescription("Photo thumbnail");
  });

  it("describes a pending thumbnail on the row control", () => {
    const entry = photoEntry({thumbnail: "pending", thumbnailUrl: null});
    render(<FileRow entry={entry} onOpen={() => undefined} />);

    const row = screen.getByRole("button", {name: `Select file ${entry.displayName}`});
    expect(row).toHaveAccessibleDescription("Photo, thumbnail is being prepared");
    // The description references real text, which every engine honours.
    const description = document.getElementById(row.getAttribute("aria-describedby")!)!;
    expect(description).toHaveTextContent("Photo, thumbnail is being prepared");
    expect(description.getAttribute("aria-label")).toBeNull();
    expect(row.querySelector("img")).toBeNull();
  });

  it.each([
    ["failed", "Video, thumbnail could not be created"],
    ["unsupported", "Video, thumbnails are not supported for this format"],
  ] as const)("describes a %s video thumbnail", (thumbnail, description) => {
    const base = fixtureEntry(8);
    const entry: EntrySummary = {
      ...base,
      displayName: "Synthetic clip.mkv",
      typeHint: "mkv",
      media: entryMediaFixture(base.id, {kind: "video", thumbnail, thumbnailUrl: null, durationMs: 1000}),
    };
    render(<FileRow entry={entry} onOpen={() => undefined} />);
    expect(screen.getByRole("button", {name: "Select file Synthetic clip.mkv"}))
      .toHaveAccessibleDescription(description);
  });

  it("classifies a media file by its extension before the catalog describes it", () => {
    const entry = {...fixtureEntry(9), typeHint: "heic", media: null};
    render(<FileRow entry={entry} onOpen={() => undefined} />);
    expect(screen.getByRole("button", {name: `Select file ${entry.displayName}`}))
      .toHaveAccessibleDescription("Photo");
  });

  it("keeps plain icons for directories, other files and extension words that are not extensions", () => {
    const directory: EntrySummary = {
      ...fixtureEntry(10), displayName: "Holiday.jpg", kind: "directory", typeHint: "jpg", media: null,
    };
    const pdf: EntrySummary = {...fixtureEntry(11), displayName: "Notes.pdf", typeHint: "pdf", media: null};
    const legacyWord: EntrySummary = {...fixtureEntry(12), displayName: "Legacy", typeHint: "image", media: null};
    const {container} = render(
      <>
        <FileRow entry={directory} onOpen={() => undefined} />
        <FileRow entry={pdf} onOpen={() => undefined} />
        <FileRow entry={legacyWord} onOpen={() => undefined} />
      </>,
    );

    expect(container.querySelector(".media-thumbnail")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    const directoryRow = screen.getByRole("button", {name: "Open directory Holiday.jpg"});
    expect(directoryRow).not.toHaveAttribute("aria-describedby");
    expect(directoryRow.querySelector(".file-row__icon--directory")).toHaveTextContent("▣");
  });

  it("names a root-wide result's folder and its action", () => {
    const entry = {...fixtureEntry(9), displayName: "IMG_0009.jpg"};
    const open = vi.fn();
    const {rerender} = render(<FileRow entry={entry} onOpen={open} location="DCIM" />);
    const row = screen.getByRole("button", {name: "Select file IMG_0009.jpg in DCIM"});
    expect(row).toHaveTextContent("in DCIM");
    expect(row).toHaveClass("file-row--located");
    // The folder name is isolated like the file name, so a right-to-left name stays whole.
    expect(row.querySelector(".file-row__location bdi")).toHaveTextContent(/^DCIM$/);

    rerender(<FileRow entry={entry} onOpen={open} location="DCIM" action="Open photo" />);
    fireEvent.click(screen.getByRole("button", {name: "Open photo IMG_0009.jpg in DCIM"}));
    expect(open).toHaveBeenCalledWith(entry);
    rerender(<FileRow entry={entry} onOpen={open} />);
    expect(screen.getByRole("button", {name: "Select file IMG_0009.jpg"})).not.toHaveTextContent("in ");
  });

  it("opens the entry when activated", () => {
    const onOpen = vi.fn();
    const entry = photoEntry();
    render(<FileRow entry={entry} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", {name: `Select file ${entry.displayName}`}));
    expect(onOpen).toHaveBeenCalledExactlyOnceWith(entry);
  });
});
