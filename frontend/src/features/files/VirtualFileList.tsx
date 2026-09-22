import {defaultRangeExtractor, useVirtualizer, type Range} from "@tanstack/react-virtual";
import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {FileRow} from "./FileRow";
import type {EntrySummary} from "./types";

const ROW_HEIGHT = 80;
const INITIAL_VIEWPORT_HEIGHT = 480;

export type VisibleAnchor = {
  id: string | null;
  offset: number;
};

export type VirtualFileListProps = {
  entries: EntrySummary[];
  onOpen(entry: EntrySummary): void;
  onLoadNext(): unknown | Promise<unknown>;
  onLoadPrevious(): unknown | Promise<unknown>;
  hasNext: boolean;
  hasPrevious: boolean;
  initialAnchor?: VisibleAnchor;
  onAnchorChange?(anchor: VisibleAnchor): void;
};

type FocusedRow = {id: string; index: number};

function nearestSurvivingIndex(
  previousEntries: EntrySummary[],
  previousIndex: number,
  entries: EntrySummary[],
): number {
  const currentIndexes = new Map(entries.map((entry, index) => [entry.id, index]));
  for (let distance = 1; distance < previousEntries.length; distance += 1) {
    const before = previousEntries[previousIndex - distance];
    if (before) {
      const index = currentIndexes.get(before.id);
      if (index !== undefined) return index;
    }
    const after = previousEntries[previousIndex + distance];
    if (after) {
      const index = currentIndexes.get(after.id);
      if (index !== undefined) return index;
    }
  }
  return entries.length === 0 ? -1 : 0;
}

export function VirtualFileList({
  entries,
  onOpen,
  onLoadNext,
  onLoadPrevious,
  hasNext,
  hasPrevious,
  initialAnchor,
  onAnchorChange,
}: VirtualFileListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const previousEntriesRef = useRef(entries);
  const scrollTopRef = useRef(0);
  const programmaticScrollTopRef = useRef<number | null>(null);
  const rowControlsRef = useRef(new Map<string, HTMLButtonElement>());
  const focusedRef = useRef<FocusedRow | null>(null);
  const pendingFocusOwnerRef = useRef<Element | null>(null);
  const pagingRef = useRef({next: false, previous: false});
  const initialAnchorRef = useRef(initialAnchor);
  const [scrollTop, setScrollTop] = useState(0);
  const [paging, setPaging] = useState({next: false, previous: false});
  const [announcement, setAnnouncement] = useState("");
  const [pendingFocusId, setPendingFocusId] = useState<string | null>(null);

  const rangeExtractor = useCallback((range: Range) => {
    const indexes = defaultRangeExtractor(range);
    const focused = focusedRef.current;
    if (!focused) return indexes;
    const currentIndex = entries.findIndex((entry) => entry.id === focused.id);
    if (currentIndex >= 0 && !indexes.includes(currentIndex)) {
      indexes.push(currentIndex);
      indexes.sort((left, right) => left - right);
    }
    return indexes;
  }, [entries]);

  const virtualizer = useVirtualizer<HTMLDivElement, HTMLLIElement>({
    count: entries.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    getItemKey: (index) => entries[index]!.id,
    overscan: 8,
    rangeExtractor,
    initialRect: {width: 320, height: INITIAL_VIEWPORT_HEIGHT},
  });
  const virtualItems = virtualizer.getVirtualItems();
  const firstVisibleIndex = entries.length === 0
    ? -1
    : Math.min(entries.length - 1, Math.max(0, Math.floor(scrollTop / ROW_HEIGHT)));
  const firstVisible = firstVisibleIndex < 0 ? null : entries[firstVisibleIndex]!;

  const publishAnchor = useCallback(() => {
    const index = entries.length === 0
      ? -1
      : Math.min(entries.length - 1, Math.max(0, Math.floor(scrollTopRef.current / ROW_HEIGHT)));
    const entry = index < 0 ? null : entries[index]!;
    onAnchorChange?.({
      id: entry?.id ?? null,
      offset: index < 0 ? 0 : index * ROW_HEIGHT - scrollTopRef.current,
    });
  }, [entries, onAnchorChange]);

  useLayoutEffect(() => {
    const viewport = scrollRef.current;
    if (!viewport) return;
    const anchor = initialAnchorRef.current;
    initialAnchorRef.current = undefined;
    if (!anchor?.id) return;
    const index = entries.findIndex((entry) => entry.id === anchor.id);
    if (index < 0) return;
    const nextOffset = Math.max(0, index * ROW_HEIGHT - anchor.offset);
    programmaticScrollTopRef.current = nextOffset;
    viewport.scrollTop = nextOffset;
    scrollTopRef.current = nextOffset;
    setScrollTop(nextOffset);
  }, [entries]);

  useLayoutEffect(() => {
    const previousEntries = previousEntriesRef.current;
    previousEntriesRef.current = entries;
    if (previousEntries === entries || previousEntries.length === 0) return;

    const viewport = scrollRef.current;
    if (!viewport) return;
    const oldFirstIndex = Math.min(
      previousEntries.length - 1,
      Math.max(0, Math.floor(scrollTopRef.current / ROW_HEIGHT)),
    );
    const oldAnchor = previousEntries[oldFirstIndex];
    const oldOffset = oldFirstIndex * ROW_HEIGHT - scrollTopRef.current;
    let newIndex = oldAnchor ? entries.findIndex((entry) => entry.id === oldAnchor.id) : -1;
    if (newIndex < 0 && entries.length > 0) {
      newIndex = nearestSurvivingIndex(previousEntries, oldFirstIndex, entries);
      setAnnouncement("The visible file moved to the nearest available file.");
    }
    if (newIndex >= 0) {
      const nextOffset = Math.max(0, newIndex * ROW_HEIGHT - oldOffset);
      programmaticScrollTopRef.current = nextOffset;
      viewport.scrollTop = nextOffset;
      scrollTopRef.current = nextOffset;
      setScrollTop(nextOffset);
    }

    const focused = focusedRef.current;
    if (!focused) return;
    const retainedFocusIndex = entries.findIndex((entry) => entry.id === focused.id);
    if (retainedFocusIndex >= 0) {
      focusedRef.current = {id: focused.id, index: retainedFocusIndex};
      return;
    }
    if (entries.length === 0) {
      focusedRef.current = null;
      return;
    }
    const previousFocusedIndex = previousEntries.findIndex((entry) => entry.id === focused.id);
    const replacementIndex = nearestSurvivingIndex(
      previousEntries,
      previousFocusedIndex >= 0 ? previousFocusedIndex : focused.index,
      entries,
    );
    const replacement = entries[replacementIndex]!;
    focusedRef.current = {id: replacement.id, index: replacementIndex};
    pendingFocusOwnerRef.current = document.activeElement;
    setPendingFocusId(replacement.id);
  }, [entries]);

  useLayoutEffect(() => {
    if (pendingFocusId === null) return;
    if (document.activeElement !== pendingFocusOwnerRef.current) {
      focusedRef.current = null;
      pendingFocusOwnerRef.current = null;
      setPendingFocusId(null);
      return;
    }
    const control = rowControlsRef.current.get(pendingFocusId);
    if (!control) return;
    control.focus({preventScroll: true});
    pendingFocusOwnerRef.current = null;
    setAnnouncement("Focus moved to the nearest available file.");
    setPendingFocusId(null);
  }, [pendingFocusId, virtualItems]);

  const load = useCallback(async (direction: "next" | "previous") => {
    if (pagingRef.current[direction]) return;
    pagingRef.current[direction] = true;
    setPaging((current) => ({...current, [direction]: true}));
    publishAnchor();
    try {
      await (direction === "next" ? onLoadNext() : onLoadPrevious());
    } finally {
      pagingRef.current[direction] = false;
      setPaging((current) => ({...current, [direction]: false}));
    }
  }, [onLoadNext, onLoadPrevious, publishAnchor]);

  const testMetrics = import.meta.env.MODE === "test" ? {
    "data-testid": "file-list-viewport",
    "data-first-visible-id": firstVisible?.id,
    "data-virtual-measurement-count": virtualizer.measurementsCache.length,
    "data-virtual-element-count": virtualizer.elementsCache.size,
  } : {};

  return (
    <div className="virtual-file-list">
      <button
        className="file-page-control interactive"
        type="button"
        disabled={!hasPrevious}
        aria-disabled={paging.previous || undefined}
        aria-busy={paging.previous || undefined}
        onClick={() => void load("previous")}
      >
        {paging.previous ? "Loading previous files…" : "Load previous files"}
      </button>
      <div
        className="virtual-file-list__viewport"
        {...testMetrics}
        ref={scrollRef}
        onScroll={(event) => {
          const viewport = event.currentTarget;
          const programmaticScrollTop = programmaticScrollTopRef.current;
          const isProgrammaticRestoration = programmaticScrollTop !== null &&
            Math.abs(viewport.scrollTop - programmaticScrollTop) < 1;
          programmaticScrollTopRef.current = null;
          scrollTopRef.current = viewport.scrollTop;
          setScrollTop(viewport.scrollTop);
          publishAnchor();
          if (isProgrammaticRestoration) return;
          if (
            hasNext && !pagingRef.current.next && viewport.scrollHeight > viewport.clientHeight &&
            viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - ROW_HEIGHT
          ) {
            void load("next");
          } else if (
            hasPrevious && !pagingRef.current.previous && viewport.scrollHeight > viewport.clientHeight &&
            viewport.scrollTop <= ROW_HEIGHT
          ) {
            void load("previous");
          }
        }}
      >
        <ul
          aria-label="Files"
          className="virtual-file-list__items"
          style={{height: virtualizer.getTotalSize(), position: "relative"}}
        >
          {virtualItems.map((row) => {
            const entry = entries[row.index]!;
            return (
              <li
                key={row.key}
                className="virtual-file-list__item"
                style={{
                  position: "absolute",
                  insetInline: 0,
                  top: 0,
                  height: ROW_HEIGHT,
                  transform: `translateY(${row.start}px)`,
                }}
              >
                <span
                  className="virtual-file-list__control"
                  ref={(node) => {
                    if (node instanceof HTMLSpanElement) {
                      const control = node.querySelector("button");
                      if (control) rowControlsRef.current.set(entry.id, control);
                    } else {
                      rowControlsRef.current.delete(entry.id);
                    }
                  }}
                  onFocus={() => { focusedRef.current = {id: entry.id, index: row.index}; }}
                  onBlur={(event) => {
                    if (
                      !event.currentTarget.contains(event.relatedTarget as Node | null) &&
                      focusedRef.current?.id === entry.id
                    ) focusedRef.current = null;
                  }}
                >
                  <FileRow entry={entry} onOpen={onOpen} />
                </span>
              </li>
            );
          })}
        </ul>
      </div>
      <button
        className="file-page-control interactive"
        type="button"
        disabled={!hasNext}
        aria-disabled={paging.next || undefined}
        aria-busy={paging.next || undefined}
        onClick={() => void load("next")}
      >
        {paging.next ? "Loading more files…" : "Load more files"}
      </button>
      <p className="visually-hidden" role="status" aria-live="polite">{announcement}</p>
    </div>
  );
}
