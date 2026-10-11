import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {MemoryRouter} from "react-router";
import {afterEach, describe, expect, it, vi} from "vitest";
import {timelineItemFixture, timelineItemId} from "./test-fixtures";
import {monthOf, SORT_SOURCE_DETAIL} from "./timeline-layout";
import {TimelineGrid, type TimelineGridProps} from "./TimelineGrid";
import type {TimelineItem} from "./types";
import {viewerPath} from "./viewer-route";

function items(start: number, length: number): TimelineItem[] {
  return Array.from({length}, (_, offset) => timelineItemFixture(start + offset));
}

function setViewport(width: number, height: number) {
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(width);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(height);
}

function gridElement(props: Partial<TimelineGridProps> = {}) {
  return (
    <MemoryRouter>
      <TimelineGrid
        items={items(0, 1000)}
        hasNext={false}
        hasPrevious={false}
        loadingNext={false}
        loadingPrevious={false}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        viewerTarget={(item) => ({to: viewerPath(item.rootId, item.id), state: null})}
        onOpen={vi.fn()}
        timeZone="UTC"
        {...props}
      />
    </MemoryRouter>
  );
}

afterEach(() => vi.restoreAllMocks());

describe("TimelineGrid", () => {
  it.each([
    ["a phone", 320, "3"],
    ["a tablet", 700, "4"],
    ["a desktop", 1200, "6"],
  ])("lays out %s in rows of %s tiles", (_name, width, columns) => {
    setViewport(width, 640);
    render(gridElement());
    expect(screen.getByTestId("timeline-viewport")).toHaveAttribute("data-columns", columns);
    expect(screen.getAllByRole("link").length).toBeGreaterThan(0);
  });

  it.each([
    ["a tall desktop", 3000],
    ["a very tall desktop", 6000],
  ])("caps the rendered tiles at 80 on %s", (_name, height) => {
    setViewport(1200, height);
    render(gridElement());

    // More than 80 tiles are visible here, so only the cap keeps the count down.
    const tiles = screen.getAllByRole("link");
    expect(tiles.length).toBeLessThanOrEqual(80);
    expect(tiles.length).toBeGreaterThanOrEqual(72);
  });

  it("keeps the window bounded and moves it while scrolling", async () => {
    setViewport(320, 640);
    render(gridElement());
    const viewport = screen.getByTestId("timeline-viewport");

    fireEvent.scroll(viewport, {target: {scrollTop: 20_000}});

    await waitFor(() => expect(screen.queryByRole("link", {name: /Synthetic photo 0000\.jpg/})).not.toBeInTheDocument());
    const tiles = screen.getAllByRole("link");
    expect(tiles.length).toBeLessThanOrEqual(80);
    expect(tiles.length).toBeGreaterThan(0);
  });

  it("labels month headers by their date source", () => {
    setViewport(320, 640);
    const captured = timelineItemFixture(0, {sortDate: "2027-03-20T10:00:00Z", sortDateSource: "captured"});
    const modified = timelineItemFixture(1, {sortDate: "2027-02-14T10:00:00Z", sortDateSource: "modified"});
    render(gridElement({items: [captured, modified]}));

    const [first, second] = screen.getAllByRole("heading", {level: 2});
    expect(first).toHaveTextContent(`${monthOf(captured.media.sortDate, "UTC").label} · ${SORT_SOURCE_DETAIL.captured}`);
    expect(second).toHaveTextContent(`${monthOf(modified.media.sortDate, "UTC").label} · ${SORT_SOURCE_DETAIL.modified}`);
    expect(second).not.toHaveTextContent(/Captured/);
  });

  it("names tiles and badges videos with their duration", () => {
    setViewport(320, 640);
    render(gridElement({items: [timelineItemFixture(0, {kind: "video"}), timelineItemFixture(1)]}));

    const video = screen.getByRole("link", {name: "Open video Synthetic video 0000.mp4, 1:01"});
    expect(within(video).getByText("▶ 1:01")).toHaveAttribute("aria-hidden", "true");
    const photo = screen.getByRole("link", {name: "Open photo Synthetic photo 0001.jpg"});
    expect(within(photo).queryByText(/▶/)).not.toBeInTheDocument();
    expect(photo).toHaveAttribute("title", "Synthetic photo 0001.jpg");
  });

  it("renders tiles that cannot be opened as plain pictures", () => {
    setViewport(320, 640);
    render(gridElement({items: items(0, 3), viewerTarget: () => null}));
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(screen.getByTitle("Synthetic photo 0001.jpg")).toBeInTheDocument();
  });

  it("opens the viewer path and reports the opened row's offset", () => {
    setViewport(320, 640);
    const onOpen = vi.fn();
    const list = items(0, 6);
    render(gridElement({items: list, onOpen}));

    const tile = screen.getByRole("link", {name: "Open photo Synthetic photo 0004.jpg"});
    expect(tile).toHaveAttribute("href", viewerPath(list[4]!.rootId, list[4]!.id));
    fireEvent.click(tile);

    // One header (56 px) and one 108 px row of three 104 px tiles precede the second row.
    expect(onOpen).toHaveBeenCalledExactlyOnceWith(list[4], 164);
  });

  it("restores focus and scroll to the tile a closed viewer showed last", async () => {
    setViewport(320, 640);
    const onRestored = vi.fn();
    render(gridElement({restore: {entryId: timelineItemId(500), offset: null}, onRestored}));

    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-entry-id", timelineItemId(500)));
    expect(onRestored).toHaveBeenCalledOnce();
    const viewport = screen.getByTestId("timeline-viewport");
    expect(viewport.scrollTop).toBeGreaterThan(0);
    expect(screen.getAllByRole("link").length).toBeLessThanOrEqual(80);
  });

  it("restores the exact offset when the viewer did not move", async () => {
    setViewport(320, 640);
    render(gridElement({restore: {entryId: timelineItemId(30), offset: 100}}));

    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-entry-id", timelineItemId(30)));
    // Item 30 is in tile row 10: 56 + 10 * 108 = 1136 from the top, shown 100 px below the viewport top.
    expect(screen.getByTestId("timeline-viewport").scrollTop).toBe(1036);
  });

  it("keeps a focused tile rendered while scrolling away", async () => {
    setViewport(320, 640);
    render(gridElement({restore: {entryId: timelineItemId(30), offset: 100}}));
    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-entry-id", timelineItemId(30)));

    fireEvent.scroll(screen.getByTestId("timeline-viewport"), {target: {scrollTop: 20_000}});

    await waitFor(() => expect(screen.queryByRole("link", {name: /Synthetic photo 0000\.jpg/})).not.toBeInTheDocument());
    expect(screen.getByRole("link", {name: "Open photo Synthetic photo 0030.jpg"})).toBe(document.activeElement);
    expect(screen.getAllByRole("link").length).toBeLessThanOrEqual(80);
  });

  it("keeps the first visible tile in place when the first page is dropped", async () => {
    setViewport(320, 640);
    const {rerender} = render(gridElement({items: items(0, 300)}));
    const viewport = screen.getByTestId("timeline-viewport");
    // Item 150 starts tile row 51: 56 + 50 * 108.
    fireEvent.scroll(viewport, {target: {scrollTop: 5456}});

    rerender(gridElement({items: items(60, 300)}));

    // Item 150 is now the 91st item, in tile row 31: 56 + 30 * 108.
    await waitFor(() => expect(viewport.scrollTop).toBe(3296));
  });

  it("loads the next page when the window does not fill the viewport", async () => {
    setViewport(320, 640);
    const onLoadNext = vi.fn();
    render(gridElement({items: items(0, 3), hasNext: true, onLoadNext}));
    await waitFor(() => expect(onLoadNext).toHaveBeenCalled());
  });

  it("offers keyboard paging buttons only when more items exist", () => {
    setViewport(320, 640);
    const onLoadNext = vi.fn();
    const onLoadPrevious = vi.fn();
    render(gridElement({items: items(0, 300), hasNext: true, hasPrevious: true, onLoadNext, onLoadPrevious}));

    fireEvent.click(screen.getByRole("button", {name: "Load older items"}));
    fireEvent.click(screen.getByRole("button", {name: "Load newer items"}));
    expect(onLoadNext).toHaveBeenCalled();
    expect(onLoadPrevious).toHaveBeenCalledOnce();
  });
});
