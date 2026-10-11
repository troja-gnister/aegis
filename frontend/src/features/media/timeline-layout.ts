import type {Range} from "@tanstack/react-virtual";
import {formatDuration} from "./format";
import type {SortDateSource, TimelineItem, TimelineKind} from "./types";

/** At most this many tiles are rendered at once, whatever the screen size. */
export const MAX_RENDERED_TILES = 80;
export const TIMELINE_GAP = 4;
export const MONTH_HEADER_HEIGHT = 56;

export type TimelineHeaderRow = {
  type: "header";
  key: string;
  /** `YYYY-MM` in the display time zone. */
  monthKey: string;
  source: SortDateSource;
  title: string;
  detail: string;
};

export type TimelineTileRow = {
  type: "tiles";
  key: string;
  items: TimelineItem[];
};

export type TimelineRow = TimelineHeaderRow | TimelineTileRow;

/** Tiles never grow past this edge, so small thumbnails are not blown up on wide screens. */
export const MAX_TILE_SIZE = 220;

/** Three columns on phones; on wider screens, as many as keep tiles within `MAX_TILE_SIZE`. */
export function columnsForWidth(width: number): number {
  if (width < 560) return 3;
  return Math.max(4, Math.ceil((width + TIMELINE_GAP) / (MAX_TILE_SIZE + TIMELINE_GAP)));
}

/** The square tile edge for a grid width, never below one touch target. */
export function tileSizeFor(width: number, columns: number): number {
  return Math.max(44, Math.floor((width - (columns - 1) * TIMELINE_GAP) / columns));
}

const keyFormatters = new Map<string, Intl.DateTimeFormat>();
const labelFormatters = new Map<string, Intl.DateTimeFormat>();

function formatter(cache: Map<string, Intl.DateTimeFormat>, timeZone: string,
                   locale: string | undefined, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const cacheKey = timeZone;
  let value = cache.get(cacheKey);
  if (!value) {
    value = new Intl.DateTimeFormat(locale, {...options, timeZone});
    cache.set(cacheKey, value);
  }
  return value;
}

/**
 * The calendar month of a sort date. Sort dates are wall-clock times encoded
 * with `Z` (see `EntryMedia.sortDate`), so they are read in UTC: the month is
 * the camera's (or the file's) own month, it matches the API's UTC `year`
 * filter, and every viewer of a root sees the same grouping. `timeZone` is for
 * tests only.
 */
export function monthOf(sortDate: string, timeZone = "UTC"): {key: string; label: string} {
  const date = new Date(sortDate);
  if (Number.isNaN(date.getTime())) return {key: "unknown", label: "Unknown month"};
  const parts = formatter(keyFormatters, timeZone, "en-US", {year: "numeric", month: "2-digit"})
    .formatToParts(date);
  const year = parts.find((part) => part.type === "year")?.value ?? "0000";
  const month = parts.find((part) => part.type === "month")?.value ?? "00";
  const label = formatter(labelFormatters, timeZone, undefined, {year: "numeric", month: "long"}).format(date);
  return {key: `${year.padStart(4, "0")}-${month}`, label};
}

/**
 * How a month group's dates were obtained. A group sorted by file
 * modification time never reads as a capture month.
 */
export const SORT_SOURCE_DETAIL: Record<SortDateSource, string> = {
  captured: "Captured",
  modified: "Modified",
};

/**
 * Grid rows for the loaded timeline: a month header for every run of items
 * that share a month and a date source, then rows of `columns` tiles. The
 * server orders by date only, so a month with both captured and modified
 * dates has one header per run, each labeled by its own source.
 */
export function buildTimelineRows(items: readonly TimelineItem[], columns: number, timeZone = "UTC"): TimelineRow[] {
  const rows: TimelineRow[] = [];
  let groupKey: string | null = null;
  let current: TimelineTileRow | null = null;
  for (const item of items) {
    const month = monthOf(item.media.sortDate, timeZone);
    const source = item.media.sortDateSource;
    const key = `${month.key}|${source}`;
    if (key !== groupKey) {
      groupKey = key;
      current = null;
      rows.push({
        type: "header",
        key: `header:${item.id}`,
        monthKey: month.key,
        source,
        title: month.label,
        detail: SORT_SOURCE_DETAIL[source],
      });
    }
    if (current === null || current.items.length >= columns) {
      current = {type: "tiles", key: `tiles:${item.id}`, items: []};
      rows.push(current);
    }
    current.items.push(item);
  }
  return rows;
}

/** Accessible name for a tile: what activating it does, the file name and, for videos, the duration. */
export function tileLabel(item: TimelineItem): string {
  if (item.media.kind === "video") {
    const duration = item.media.durationMs === null ? "" : `, ${formatDuration(item.media.durationMs)}`;
    return `Open video ${item.displayName}${duration}`;
  }
  return `Open photo ${item.displayName}`;
}

export const KIND_FILTER_LABEL: Record<TimelineKind, string> = {
  all: "All",
  photo: "Photos",
  video: "Videos",
};

export const KIND_FILTER_ANNOUNCEMENT: Record<TimelineKind, string> = {
  all: "Showing photos and videos.",
  photo: "Showing photos only.",
  video: "Showing videos only.",
};

function tilesIn(rows: readonly TimelineRow[], index: number): number {
  const row = rows[index];
  return row?.type === "tiles" ? row.items.length : 0;
}

/**
 * The rows to render: the visible range, grown by overscan while the total
 * stays within `maxTiles`, plus a pinned row (the focused or restored tile)
 * so focus is never lost to virtualization.
 */
export function boundedRowIndexes(
  range: Range,
  rows: readonly TimelineRow[],
  maxTiles: number,
  pinnedIndex: number | null,
): number[] {
  if (range.count === 0 || rows.length === 0) return [];
  let start = Math.max(0, Math.min(range.startIndex, rows.length - 1));
  let end = Math.max(start, Math.min(range.endIndex, rows.length - 1));
  const pinned = pinnedIndex !== null && pinnedIndex >= 0 && pinnedIndex < rows.length ? pinnedIndex : null;
  const pinnedOutside = pinned !== null && (pinned < start || pinned > end);
  const budget = Math.max(0, maxTiles - (pinnedOutside ? tilesIn(rows, pinned) : 0));
  let tiles = 0;
  for (let index = start; index <= end; index += 1) tiles += tilesIn(rows, index);
  // Never on a sane screen: a visible range over budget is cut at its end.
  while (tiles > budget && end > start) {
    tiles -= tilesIn(rows, end);
    end -= 1;
  }
  for (let step = 0; step < range.overscan; step += 1) {
    const below = end + 1;
    if (below < rows.length && tiles + tilesIn(rows, below) <= budget) {
      end = below;
      tiles += tilesIn(rows, below);
    }
    const above = start - 1;
    if (above >= 0 && tiles + tilesIn(rows, above) <= budget) {
      start = above;
      tiles += tilesIn(rows, above);
    }
  }
  const indexes: number[] = [];
  for (let index = start; index <= end; index += 1) indexes.push(index);
  if (pinned !== null && (pinned < start || pinned > end)) {
    indexes.push(pinned);
    indexes.sort((left, right) => left - right);
  }
  return indexes;
}

/** The index of the row whose top is the last at or above `offset` (binary search over row tops). */
export function rowAt(starts: readonly number[], offset: number): number {
  let low = 0;
  let high = starts.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (starts[middle]! <= offset) low = middle;
    else high = middle - 1;
  }
  return low;
}
