import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {useLayoutEffect, useState} from "react";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {fixtureEntry} from "./test-fixtures";
import {VirtualFileList} from "./VirtualFileList";
import "./files.css";

function entries(start = 0, length = 500) {
  return Array.from({length}, (_, index) => fixtureEntry(start + index));
}

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(480);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(320);
});

afterEach(() => vi.restoreAllMocks());

describe("VirtualFileList", () => {
  it("renders a bounded row window and offers keyboard paging", () => {
    render(
      <VirtualFileList
        entries={entries()}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );

    expect(screen.getAllByRole("listitem").length).toBeLessThanOrEqual(80);
    expect(screen.getByRole("button", {name: "Load more files"})).toBeEnabled();
    expect(screen.getByRole("button", {name: "Load previous files"})).toBeEnabled();
    expect(screen.getByRole("list", {name: "Files"})).not.toHaveAttribute("aria-setsize");
  });

  it("renders full accessible labels while visually bounding long and RTL names", () => {
    const longName = `Archive-${"long-unbroken-name".repeat(20)}`;
    const rtlName = "ملف العائلة.jpg";
    const items = [
      {...fixtureEntry(0), displayName: longName},
      {...fixtureEntry(1), displayName: rtlName, typeHint: "video"},
      {...fixtureEntry(2), displayName: "Documents", kind: "directory" as const, typeHint: null},
    ];
    const onOpen = vi.fn();
    render(
      <VirtualFileList
        entries={items}
        onOpen={onOpen}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext={false}
        hasPrevious={false}
      />,
    );

    const file = screen.getByRole("button", {name: `Select file ${longName}`});
    const directory = screen.getByRole("button", {name: "Open directory Documents"});
    expect(file).toHaveAttribute("title", longName);
    expect(screen.getByText(rtlName)).toHaveAttribute("dir", "auto");
    expect(getComputedStyle(screen.getByText(longName)).overflow).toBe("hidden");
    fireEvent.click(directory);
    expect(onOpen).toHaveBeenCalledWith(items[2]);
  });

  it("restores the first visible retained row after front eviction", async () => {
    const initial = entries();
    const {rerender} = render(
      <VirtualFileList
        entries={initial}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );
    const viewport = screen.getByTestId("file-list-viewport");
    Object.defineProperty(viewport, "clientHeight", {configurable: true, value: 480});
    fireEvent.scroll(viewport, {target: {scrollTop: 160}});

    await waitFor(() => expect(viewport).toHaveAttribute("data-first-visible-id", initial[2]!.id));
    rerender(
      <VirtualFileList
        entries={entries(1)}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );

    await waitFor(() => expect(viewport.scrollTop).toBe(80));
    expect(viewport).toHaveAttribute("data-first-visible-id", initial[2]!.id);
  });

  it.each([
    ["front", 0, 2, 100, 100, 0],
    ["back", 100, 498, 0, 499, 499],
  ])("moves a removed %s anchor to the nearest prior-order neighbor", async (
    _direction,
    initialStart,
    anchorIndex,
    nextStart,
    expectedEntryIndex,
    expectedNewIndex,
  ) => {
    const initial = entries(initialStart);
    const {rerender} = render(
      <VirtualFileList
        entries={initial}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );
    const viewport = screen.getByTestId("file-list-viewport");
    Object.defineProperty(viewport, "clientHeight", {configurable: true, value: 480});
    fireEvent.scroll(viewport, {target: {scrollTop: anchorIndex * 80}});
    await waitFor(() => expect(viewport).toHaveAttribute(
      "data-first-visible-id",
      initial[anchorIndex]!.id,
    ));

    const retained = entries(nextStart);
    rerender(
      <VirtualFileList
        entries={retained}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );

    const expected = fixtureEntry(expectedEntryIndex);
    await waitFor(() => expect(viewport.scrollTop).toBe(expectedNewIndex * 80));
    expect(viewport).toHaveAttribute("data-first-visible-id", expected.id);
    expect(screen.getByRole("status")).toHaveTextContent(/visible file moved to the nearest available file/i);
  });

  it.each([
    ["front", 0, 2, 100, 100],
    ["back", 100, 498, 0, 499],
  ])("moves %s-evicted owned focus to the nearest prior-order neighbor", async (
    _direction,
    initialStart,
    focusIndex,
    nextStart,
    expectedEntryIndex,
  ) => {
    const initial = entries(initialStart);
    const {rerender} = render(
      <VirtualFileList
        entries={initial}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );
    const viewport = screen.getByTestId("file-list-viewport");
    Object.defineProperty(viewport, "clientHeight", {configurable: true, value: 480});
    fireEvent.scroll(viewport, {target: {scrollTop: focusIndex * 80}});
    const focused = await screen.findByRole("button", {
      name: `Select file ${initial[focusIndex]!.displayName}`,
    });
    focused.focus();

    const retained = entries(nextStart);
    rerender(
      <VirtualFileList
        entries={retained}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );

    const expected = fixtureEntry(expectedEntryIndex);
    await waitFor(() => expect(screen.getByRole("button", {
      name: `Select file ${expected.displayName}`,
    })).toHaveFocus());
    expect(screen.getByRole("status")).toHaveTextContent(/focus moved to the nearest available file/i);
  });

  it("renders and focuses an offscreen nearest neighbor after the owned row is evicted", async () => {
    const initial = entries(100);
    const {rerender} = render(
      <VirtualFileList
        entries={initial}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );
    const viewport = screen.getByTestId("file-list-viewport");
    Object.defineProperty(viewport, "clientHeight", {configurable: true, value: 480});
    fireEvent.scroll(viewport, {target: {scrollTop: 60 * 80}});
    const ownedRow = await screen.findByRole("button", {name: "Select file Synthetic file 0160.jpg"});
    ownedRow.focus();
    fireEvent.scroll(viewport, {target: {scrollTop: 494 * 80}});
    await waitFor(() => expect(viewport).toHaveAttribute(
      "data-first-visible-id",
      fixtureEntry(594).id,
    ));

    rerender(
      <VirtualFileList
        entries={entries(200)}
        onOpen={vi.fn()}
        onLoadNext={vi.fn()}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious
      />,
    );

    await waitFor(() => expect(screen.getByRole("button", {
      name: "Select file Synthetic file 0200.jpg",
    })).toHaveFocus());
    expect(viewport).toHaveAttribute("data-first-visible-id", fixtureEntry(594).id);
    expect(screen.getByRole("status")).toHaveTextContent(/focus moved to the nearest available file/i);
  });

  it.each(["Load more files", "Outside control"])(
    "does not fulfill deferred row focus after %s gains ownership",
    async (focusTarget) => {
      function FocusAfterEviction({active}: {active: boolean}) {
        useLayoutEffect(() => {
          if (active) screen.getByRole("button", {name: focusTarget}).focus();
        }, [active]);
        return null;
      }

      const view = (items: ReturnType<typeof entries>, ownershipChanges: boolean) => (
        <>
          <button type="button">Outside control</button>
          <VirtualFileList
            entries={items}
            onOpen={vi.fn()}
            onLoadNext={vi.fn()}
            onLoadPrevious={vi.fn()}
            hasNext
            hasPrevious
          />
          <FocusAfterEviction active={ownershipChanges} />
        </>
      );
      const {rerender} = render(view(entries(100), false));
      const viewport = screen.getByTestId("file-list-viewport");
      Object.defineProperties(viewport, {
        clientHeight: {configurable: true, value: 480},
        scrollHeight: {configurable: true, value: 40_000},
      });
      fireEvent.scroll(viewport, {target: {scrollTop: 60 * 80}});
      const ownedRow = await screen.findByRole("button", {name: "Select file Synthetic file 0160.jpg"});
      ownedRow.focus();
      fireEvent.scroll(viewport, {target: {scrollTop: 494 * 80}});
      await waitFor(() => expect(viewport).toHaveAttribute(
        "data-first-visible-id",
        fixtureEntry(594).id,
      ));

      rerender(view(entries(200), true));
      const newOwner = screen.getByRole("button", {name: focusTarget});
      expect(newOwner).toHaveFocus();

      fireEvent.scroll(viewport, {target: {scrollTop: 0}});

      await waitFor(() => expect(screen.getByRole("button", {
        name: "Select file Synthetic file 0200.jpg",
      })).toBeVisible());
      expect(newOwner).toHaveFocus();
    },
  );

  it.each(["Load more files", "Outside control"])(
    "does not steal focus from %s when a previously focused row is evicted",
    async (focusTarget) => {
      const initial = entries();
      const view = (items: ReturnType<typeof entries>) => (
        <>
          <button type="button">Outside control</button>
          <VirtualFileList
            entries={items}
            onOpen={vi.fn()}
            onLoadNext={vi.fn()}
            onLoadPrevious={vi.fn()}
            hasNext
            hasPrevious
          />
        </>
      );
      const {rerender} = render(view(initial));
      screen.getByRole("button", {name: `Select file ${initial[2]!.displayName}`}).focus();
      const destination = screen.getByRole("button", {name: focusTarget});
      destination.focus();

      rerender(view(entries(100)));

      await waitFor(() => expect(destination).toHaveFocus());
      expect(screen.getByRole("status")).not.toHaveTextContent(/focus moved/i);
    },
  );

  it.each([
    ["previous", 0, 2 * 80, 100, 0, "Load previous files"],
    ["next", 100, 394 * 80, 0, 494 * 80, "Load more files"],
  ])("does not treat a programmatic %s restoration scroll as user paging", async (
    direction,
    initialStart,
    initialScrollTop,
    nextStart,
    restoredScrollTop,
    controlName,
  ) => {
    const loadNext = vi.fn();
    const loadPrevious = vi.fn();
    const {rerender} = render(
      <VirtualFileList
        entries={entries(initialStart)}
        onOpen={vi.fn()}
        onLoadNext={loadNext}
        onLoadPrevious={loadPrevious}
        hasNext
        hasPrevious
      />,
    );
    const viewport = screen.getByTestId("file-list-viewport");
    Object.defineProperties(viewport, {
      clientHeight: {configurable: true, value: 480},
      scrollHeight: {configurable: true, value: 40_000},
    });
    fireEvent.scroll(viewport, {target: {scrollTop: initialScrollTop}});
    rerender(
      <VirtualFileList
        entries={entries(nextStart)}
        onOpen={vi.fn()}
        onLoadNext={loadNext}
        onLoadPrevious={loadPrevious}
        hasNext
        hasPrevious
      />,
    );
    await waitFor(() => expect(viewport.scrollTop).toBe(restoredScrollTop));

    fireEvent.scroll(viewport, {target: {scrollTop: restoredScrollTop}});
    expect(loadNext).not.toHaveBeenCalled();
    expect(loadPrevious).not.toHaveBeenCalled();

    const awayFromEdge = direction === "previous" ? 2 * 80 : 490 * 80;
    fireEvent.scroll(viewport, {target: {scrollTop: awayFromEdge}});
    fireEvent.scroll(viewport, {target: {scrollTop: restoredScrollTop}});
    const expectedLoader = direction === "previous" ? loadPrevious : loadNext;
    await waitFor(() => expect(expectedLoader).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", {name: controlName})).toBeVisible();
  });

  it("keeps DOM and virtualizer caches bounded through repeated page eviction", async () => {
    function EvictionFixture() {
      const [start, setStart] = useState(0);
      return (
        <>
          <button type="button" onClick={() => setStart((value) => value + 100)}>Evict page</button>
          <VirtualFileList
            entries={entries(start)}
            onOpen={vi.fn()}
            onLoadNext={vi.fn()}
            onLoadPrevious={vi.fn()}
            hasNext
            hasPrevious
          />
        </>
      );
    }
    render(<EvictionFixture />);

    for (let page = 0; page < 12; page += 1) {
      fireEvent.click(screen.getByRole("button", {name: "Evict page"}));
    }

    await waitFor(() => {
      const viewport = screen.getByTestId("file-list-viewport");
      expect(Number(viewport.dataset.virtualMeasurementCount)).toBeLessThanOrEqual(500);
      expect(Number(viewport.dataset.virtualElementCount)).toBeLessThanOrEqual(80);
      expect(screen.getAllByRole("listitem").length).toBeLessThanOrEqual(80);
    });
  });

  it.each([
    ["next", "Load more files"],
    ["previous", "Load previous files"],
  ])("keeps the %s paging control focused while preventing duplicate requests", async (
    direction,
    controlName,
  ) => {
    let release = () => {};
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const loadNext = vi.fn(() => pending);
    const loadPrevious = vi.fn(() => pending);
    render(
      <VirtualFileList
        entries={entries(0, 5)}
        onOpen={vi.fn()}
        onLoadNext={loadNext}
        onLoadPrevious={loadPrevious}
        hasNext
        hasPrevious
      />,
    );
    const control = screen.getByRole("button", {name: controlName});
    control.focus();
    fireEvent.click(control);
    fireEvent.click(control);

    const expectedLoader = direction === "next" ? loadNext : loadPrevious;
    expect(expectedLoader).toHaveBeenCalledTimes(1);
    expect(control).toHaveFocus();
    expect(control).not.toBeDisabled();
    expect(control).toHaveAttribute("aria-disabled", "true");
    release();
    await waitFor(() => expect(control).not.toHaveAttribute("aria-disabled"));
    expect(control).toHaveFocus();
  });
});
