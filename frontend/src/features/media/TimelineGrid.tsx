import {useVirtualizer, type Range} from "@tanstack/react-virtual";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import {Link} from "react-router";
import {formatDuration} from "./format";
import {MediaThumbnail} from "./MediaThumbnail";
import {
  boundedRowIndexes,
  buildTimelineRows,
  columnsForWidth,
  MAX_RENDERED_TILES,
  MONTH_HEADER_HEIGHT,
  rowAt,
  TIMELINE_GAP,
  tileLabel,
  tileSizeFor,
} from "./timeline-layout";
import type {MediaViewerState, TimelineItem} from "./types";
import "./photos.css";

const INITIAL_WIDTH = 320;
const INITIAL_HEIGHT = 480;
const OVERSCAN_ROWS = 2;

export type TimelineGridProps = {
  items: TimelineItem[];
  hasNext: boolean;
  hasPrevious: boolean;
  loadingNext: boolean;
  loadingPrevious: boolean;
  onLoadNext(): void;
  onLoadPrevious(): void;
  /** The viewer route and its history state for an item, or null when items cannot be opened. */
  viewerTarget(item: TimelineItem): {to: string; state: MediaViewerState | null} | null;
  /** Called as a tile is activated, with its row's offset from the top of the viewport. */
  onOpen(item: TimelineItem, anchorOffset: number): void;
  /** Focus and scroll to restore after the viewer closes; a null offset centers the row. */
  restore?: {entryId: string; offset: number | null};
  /** Called once the restore target was focused, or could not be found. */
  onRestored?(): void;
  /** Time zone for month headers; UTC (wall-clock sort dates) except in tests. */
  timeZone?: string;
};

type Anchor = {entryId: string; delta: number};

/**
 * A virtualized grid of the loaded timeline: month headers labeled by date
 * source and rows of square tiles (three on phones, up to six on wide
 * screens). At most about 80 tiles are rendered and images load lazily, so
 * off-screen tiles rarely start a download. The first visible tile stays in
 * place when pages are added or dropped at either end.
 */
export function TimelineGrid({
  items,
  hasNext,
  hasPrevious,
  loadingNext,
  loadingPrevious,
  onLoadNext,
  onLoadPrevious,
  viewerTarget,
  onOpen,
  restore,
  onRestored,
  timeZone = "UTC",
}: TimelineGridProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const tileRefs = useRef(new Map<string, HTMLElement>());
  const anchorRef = useRef<Anchor | null>(null);
  const focusedIdRef = useRef<string | null>(null);
  const programmaticScrollTopRef = useRef<number | null>(null);
  const restoreRef = useRef(restore);
  const onRestoredRef = useRef(onRestored);
  const [size, setSize] = useState({width: INITIAL_WIDTH, height: INITIAL_HEIGHT});
  const [measured, setMeasured] = useState(false);
  const [pendingFocusId, setPendingFocusId] = useState<string | null>(null);

  useEffect(() => {
    onRestoredRef.current = onRestored;
  });

  const columns = columnsForWidth(size.width);
  const tileSize = tileSizeFor(size.width, columns);
  const rowHeight = tileSize + TIMELINE_GAP;
  const maxTiles = MAX_RENDERED_TILES;
  const rows = useMemo(() => buildTimelineRows(items, columns, timeZone), [columns, items, timeZone]);
  const layout = useMemo(() => {
    const starts: number[] = [];
    const rowOfEntry = new Map<string, number>();
    let offset = 0;
    rows.forEach((row, index) => {
      starts.push(offset);
      if (row.type === "tiles") {
        for (const item of row.items) rowOfEntry.set(item.id, index);
        offset += rowHeight;
      } else {
        offset += MONTH_HEADER_HEIGHT;
      }
    });
    return {starts, rowOfEntry, total: offset};
  }, [rowHeight, rows]);
  const previousLayoutRef = useRef(layout);

  const anchorFor = useCallback((scrollTop: number): Anchor | null => {
    if (rows.length === 0) return null;
    for (let index = rowAt(layout.starts, scrollTop); index < rows.length; index += 1) {
      const row = rows[index]!;
      if (row.type === "tiles") return {entryId: row.items[0]!.id, delta: scrollTop - layout.starts[index]!};
    }
    return null;
  }, [layout, rows]);

  const rangeExtractor = useCallback((range: Range) => {
    const pinnedId = pendingFocusId ?? focusedIdRef.current;
    const pinnedIndex = pinnedId === null ? null : layout.rowOfEntry.get(pinnedId) ?? null;
    return boundedRowIndexes(range, rows, maxTiles, pinnedIndex);
  }, [layout, maxTiles, pendingFocusId, rows]);

  const virtualizer = useVirtualizer<HTMLDivElement, HTMLDivElement>({
    count: rows.length,
    getScrollElement: () => viewportRef.current,
    estimateSize: (index) => rows[index]?.type === "tiles" ? rowHeight : MONTH_HEADER_HEIGHT,
    getItemKey: (index) => rows[index]!.key,
    overscan: OVERSCAN_ROWS,
    rangeExtractor,
    initialRect: {width: INITIAL_WIDTH, height: INITIAL_HEIGHT},
  });
  const virtualRows = virtualizer.getVirtualItems();

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const measure = () => {
      const width = viewport.clientWidth || viewport.offsetWidth || INITIAL_WIDTH;
      const height = viewport.clientHeight || viewport.offsetHeight || INITIAL_HEIGHT;
      setSize((current) => current.width === width && current.height === height ? current : {width, height});
      setMeasured(true);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    // Row sizes follow the grid width; recompute the virtualizer's offsets.
    virtualizer.measure();
  }, [rowHeight, virtualizer]);

  const setScrollTop = useCallback((top: number) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    programmaticScrollTopRef.current = top;
    viewport.scrollTop = top;
  }, []);

  useLayoutEffect(() => {
    // Keep the first visible tile in place when pages are added or dropped, or columns change.
    const previous = previousLayoutRef.current;
    previousLayoutRef.current = layout;
    if (previous === layout) return;
    const anchor = anchorRef.current;
    if (!anchor) return;
    const index = layout.rowOfEntry.get(anchor.entryId);
    if (index === undefined) {
      anchorRef.current = anchorFor(viewportRef.current?.scrollTop ?? 0);
      return;
    }
    const top = Math.max(0, layout.starts[index]! + anchor.delta);
    if (Math.abs((viewportRef.current?.scrollTop ?? 0) - top) >= 1) setScrollTop(top);
  }, [anchorFor, layout, setScrollTop]);

  useLayoutEffect(() => {
    // Restore after the first measurement, so the row lands where the viewport really is.
    const target = restoreRef.current;
    if (!target || !measured) return;
    restoreRef.current = undefined;
    const index = layout.rowOfEntry.get(target.entryId);
    if (index === undefined) {
      onRestoredRef.current?.();
      return;
    }
    const centered = Math.max(0, (size.height - rowHeight) / 2);
    const top = Math.max(0, layout.starts[index]! - (target.offset ?? centered));
    setScrollTop(top);
    anchorRef.current = {entryId: target.entryId, delta: top - layout.starts[index]!};
    setPendingFocusId(target.entryId);
  }, [layout, measured, rowHeight, setScrollTop, size.height]);

  useLayoutEffect(() => {
    if (pendingFocusId === null) return;
    const tile = tileRefs.current.get(pendingFocusId);
    if (!tile) return;
    tile.focus({preventScroll: true});
    focusedIdRef.current = pendingFocusId;
    setPendingFocusId(null);
    onRestoredRef.current?.();
  }, [pendingFocusId, virtualRows]);

  useEffect(() => {
    // A window shorter than the viewport loads the next page (one page at a time).
    if (hasNext && !loadingNext && layout.total <= size.height + rowHeight) onLoadNext();
  }, [hasNext, layout.total, loadingNext, onLoadNext, rowHeight, size.height]);

  const testMetrics = import.meta.env.MODE === "test" ? {
    "data-testid": "timeline-viewport",
    "data-columns": columns,
  } : {};

  return (
    <div className="timeline-grid">
      {hasPrevious ? (
        <button
          className="file-page-control interactive"
          type="button"
          aria-disabled={loadingPrevious || undefined}
          aria-busy={loadingPrevious || undefined}
          onClick={() => { if (!loadingPrevious) onLoadPrevious(); }}
        >
          {loadingPrevious ? "Loading newer items…" : "Load newer items"}
        </button>
      ) : null}
      <div
        className="timeline-grid__viewport"
        ref={viewportRef}
        {...testMetrics}
        onScroll={(event) => {
          const viewport = event.currentTarget;
          const programmatic = programmaticScrollTopRef.current;
          programmaticScrollTopRef.current = null;
          anchorRef.current = anchorFor(viewport.scrollTop);
          if (programmatic !== null && Math.abs(viewport.scrollTop - programmatic) < 1) return;
          const threshold = 2 * rowHeight;
          if (viewport.scrollHeight <= viewport.clientHeight) return;
          if (hasNext && !loadingNext &&
              viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - threshold) {
            onLoadNext();
          } else if (hasPrevious && !loadingPrevious && viewport.scrollTop <= threshold) {
            onLoadPrevious();
          }
        }}
      >
        <div className="timeline-grid__items" style={{height: virtualizer.getTotalSize()}}>
          {virtualRows.map((virtualRow) => {
            const row = rows[virtualRow.index]!;
            if (row.type === "header") {
              return (
                <h2
                  key={virtualRow.key}
                  className="timeline-grid__month"
                  data-source={row.source}
                  style={{height: MONTH_HEADER_HEIGHT, transform: `translateY(${virtualRow.start}px)`}}
                >
                  <span className="timeline-grid__month-title">{row.title}</span>
                  <span className="timeline-grid__month-source"> · {row.detail}</span>
                </h2>
              );
            }
            const rowStyle = {
              "--timeline-columns": String(columns),
              "--timeline-tile": `${tileSize}px`,
              height: rowHeight,
              transform: `translateY(${virtualRow.start}px)`,
            } as CSSProperties;
            return (
              <div key={virtualRow.key} className="timeline-grid__row" style={rowStyle}>
                {row.items.map((item) => {
                  const target = viewerTarget(item);
                  const register = (node: HTMLElement | null) => {
                    if (node) tileRefs.current.set(item.id, node);
                    else tileRefs.current.delete(item.id);
                  };
                  const contents = (
                    <>
                      <MediaThumbnail entry={item} size={tileSize} className="timeline-tile__thumbnail" />
                      {item.media.kind === "video" ? (
                        <span className="timeline-tile__badge" aria-hidden="true">
                          ▶{item.media.durationMs === null ? "" : ` ${formatDuration(item.media.durationMs)}`}
                        </span>
                      ) : null}
                    </>
                  );
                  if (target === null) {
                    // Without PREVIEW the viewer could show nothing, so the tile is not a control.
                    return (
                      <span
                        key={item.id}
                        ref={register}
                        className="timeline-tile timeline-tile--static"
                        title={item.displayName}
                        data-entry-id={item.id}
                      >
                        {contents}
                      </span>
                    );
                  }
                  return (
                    <Link
                      key={item.id}
                      ref={register}
                      className="timeline-tile"
                      to={target.to}
                      state={target.state}
                      aria-label={tileLabel(item)}
                      title={item.displayName}
                      data-entry-id={item.id}
                      draggable={false}
                      onClick={() => {
                        const viewport = viewportRef.current;
                        const start = layout.starts[virtualRow.index] ?? 0;
                        onOpen(item, viewport ? start - viewport.scrollTop : 0);
                      }}
                      onFocus={() => { focusedIdRef.current = item.id; }}
                      onBlur={() => {
                        if (focusedIdRef.current === item.id) focusedIdRef.current = null;
                      }}
                    >
                      {contents}
                    </Link>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
      {hasNext ? (
        <button
          className="file-page-control interactive"
          type="button"
          aria-disabled={loadingNext || undefined}
          aria-busy={loadingNext || undefined}
          onClick={() => { if (!loadingNext) onLoadNext(); }}
        >
          {loadingNext ? "Loading older items…" : "Load older items"}
        </button>
      ) : null}
    </div>
  );
}
