import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {useState} from "react";
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

  it("moves focus to the nearest retained row and announces an evicted focus target", async () => {
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
    screen.getByRole("button", {name: `Select file ${initial[2]!.displayName}`}).focus();

    const retained = entries(100);
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

    await waitFor(() => expect(screen.getByRole("button", {
      name: `Select file ${retained[2]!.displayName}`,
    })).toHaveFocus());
    expect(screen.getByRole("status")).toHaveTextContent(/moved to the nearest available file/i);
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

  it("prevents duplicate paging requests while each direction is in flight", async () => {
    let release = () => {};
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const loadNext = vi.fn(() => pending);
    render(
      <VirtualFileList
        entries={entries(0, 5)}
        onOpen={vi.fn()}
        onLoadNext={loadNext}
        onLoadPrevious={vi.fn()}
        hasNext
        hasPrevious={false}
      />,
    );
    const next = screen.getByRole("button", {name: "Load more files"});
    fireEvent.click(next);
    fireEvent.click(next);

    expect(loadNext).toHaveBeenCalledTimes(1);
    expect(next).toBeDisabled();
    release();
    await waitFor(() => expect(next).toBeEnabled());
  });
});
