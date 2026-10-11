import {describe, expect, it} from "vitest";
import {setTestTimeZone} from "../files/test-fixtures";
import {timelineItemFixture} from "./test-fixtures";
import {
  boundedRowIndexes,
  buildTimelineRows,
  columnsForWidth,
  MAX_RENDERED_TILES,
  MAX_TILE_SIZE,
  monthOf,
  rowAt,
  SORT_SOURCE_DETAIL,
  tileLabel,
  tileSizeFor,
  type TimelineHeaderRow,
  type TimelineRow,
  type TimelineTileRow,
} from "./timeline-layout";

function headers(rows: TimelineRow[]): TimelineHeaderRow[] {
  return rows.filter((row): row is TimelineHeaderRow => row.type === "header");
}

function tileRows(rows: TimelineRow[]): TimelineTileRow[] {
  return rows.filter((row): row is TimelineTileRow => row.type === "tiles");
}

describe("timeline grid columns", () => {
  it("uses three columns on phones and more on wider screens, keeping tiles within the cap", () => {
    expect([320, 375, 414, 559].map(columnsForWidth)).toEqual([3, 3, 3, 3]);
    expect(columnsForWidth(560)).toBe(4);
    expect(columnsForWidth(800)).toBe(4);
    expect(columnsForWidth(1000)).toBe(5);
    expect(columnsForWidth(1200)).toBe(6);
    expect(columnsForWidth(2560)).toBe(12);
    for (const width of [560, 800, 1000, 1200, 1600, 2560]) {
      expect(tileSizeFor(width, columnsForWidth(width))).toBeLessThanOrEqual(MAX_TILE_SIZE);
    }
  });

  it("sizes square tiles to fill the row with fixed gaps, never below a touch target", () => {
    expect(tileSizeFor(320, 3)).toBe(104);
    expect(tileSizeFor(1000, 6)).toBe(163);
    expect(tileSizeFor(100, 3)).toBe(44);
  });
});

describe("timeline month headers", () => {
  it("reads wall-clock sort dates in UTC by default, whatever the browser's zone", () => {
    const restore = setTestTimeZone("America/New_York");
    try {
      // A photo taken at 00:30 on New Year's Day (camera clock) stays in January.
      expect(monthOf("2027-01-01T00:30:00Z").key).toBe("2027-01");
      expect(buildTimelineRows([timelineItemFixture(0, {sortDate: "2027-01-01T00:30:00Z"})], 3)[0])
        .toMatchObject({type: "header", monthKey: "2027-01"});
    } finally {
      restore();
    }
  });

  it("groups by month in an explicit time zone (tests only)", () => {
    expect(monthOf("2027-02-01T03:00:00Z", "UTC").key).toBe("2027-02");
    // 03:00 UTC on February 1 is still January 31 in New York.
    expect(monthOf("2027-02-01T03:00:00Z", "America/New_York").key).toBe("2027-01");
    expect(monthOf("2027-02-01T03:00:00Z", "UTC").label).toMatch(/2027/);
  });

  it("labels a captured month as captured and a modified month as file-modified", () => {
    const rows = buildTimelineRows([
      timelineItemFixture(0, {sortDate: "2027-03-20T10:00:00Z", sortDateSource: "captured"}),
      timelineItemFixture(1, {sortDate: "2027-03-02T10:00:00Z", sortDateSource: "captured"}),
      timelineItemFixture(2, {sortDate: "2027-02-14T10:00:00Z", sortDateSource: "modified"}),
    ], 3, "UTC");

    const [march, february] = headers(rows);
    expect(march).toMatchObject({monthKey: "2027-03", source: "captured", detail: SORT_SOURCE_DETAIL.captured});
    expect(february).toMatchObject({monthKey: "2027-02", source: "modified", detail: SORT_SOURCE_DETAIL.modified});
    expect(february!.detail).toBe("Modified");
    expect(february!.detail).not.toMatch(/captured/i);
    expect(march!.detail).not.toMatch(/modified/i);
  });

  it("never puts a modified date under a captured header within one month", () => {
    const rows = buildTimelineRows([
      timelineItemFixture(0, {sortDate: "2027-03-20T10:00:00Z", sortDateSource: "captured"}),
      timelineItemFixture(1, {sortDate: "2027-03-15T10:00:00Z", sortDateSource: "modified"}),
      timelineItemFixture(2, {sortDate: "2027-03-10T10:00:00Z", sortDateSource: "captured"}),
    ], 3, "UTC");

    expect(headers(rows).map((row) => [row.monthKey, row.source])).toEqual([
      ["2027-03", "captured"],
      ["2027-03", "modified"],
      ["2027-03", "captured"],
    ]);
    // Every tile row sits under a header of its own items' source.
    let source: string | null = null;
    for (const row of rows) {
      if (row.type === "header") source = row.source;
      else for (const item of row.items) expect(item.media.sortDateSource).toBe(source);
    }
  });

  it("splits a group into rows of the column count and keeps keys unique", () => {
    const items = Array.from({length: 7}, (_, index) =>
      timelineItemFixture(index, {sortDate: `2027-03-${String(20 - index).padStart(2, "0")}T10:00:00Z`}));
    const rows = buildTimelineRows(items, 3, "UTC");

    expect(headers(rows)).toHaveLength(1);
    expect(tileRows(rows).map((row) => row.items.length)).toEqual([3, 3, 1]);
    expect(new Set(rows.map((row) => row.key)).size).toBe(rows.length);
  });
});

describe("timeline tile labels", () => {
  it("names the action, the file and a video's duration", () => {
    expect(tileLabel(timelineItemFixture(1))).toBe("Open photo Synthetic photo 0001.jpg");
    expect(tileLabel(timelineItemFixture(2, {kind: "video"}))).toBe("Open video Synthetic video 0002.mp4, 1:01");
    expect(tileLabel(timelineItemFixture(3, {kind: "video", media: {durationMs: null}})))
      .toBe("Open video Synthetic video 0003.mp4");
  });
});

describe("timeline windowing", () => {
  const rows = buildTimelineRows(
    Array.from({length: 1200}, (_, index) => timelineItemFixture(index)),
    6,
    "UTC",
  );

  function renderedTiles(indexes: number[]): number {
    return indexes.reduce((total, index) => {
      const row = rows[index]!;
      return total + (row.type === "tiles" ? row.items.length : 0);
    }, 0);
  }

  it("grows the visible range by overscan only within the tile budget", () => {
    const indexes = boundedRowIndexes({startIndex: 40, endIndex: 45, overscan: 2, count: rows.length}, rows, 80, null);
    expect(indexes[0]).toBe(38);
    expect(indexes.at(-1)).toBe(47);
    expect(renderedTiles(indexes)).toBeLessThanOrEqual(MAX_RENDERED_TILES);
  });

  it("caps a huge visible range at the budget", () => {
    const indexes = boundedRowIndexes({startIndex: 10, endIndex: 60, overscan: 4, count: rows.length}, rows, 80, null);
    expect(renderedTiles(indexes)).toBeLessThanOrEqual(80);
    expect(indexes[0]).toBe(10);
  });

  it("keeps a pinned (focused) row rendered without exceeding the budget", () => {
    const indexes = boundedRowIndexes({startIndex: 10, endIndex: 60, overscan: 4, count: rows.length}, rows, 80, 150);
    expect(indexes).toContain(150);
    expect(renderedTiles(indexes)).toBeLessThanOrEqual(80);
    expect([...indexes].sort((left, right) => left - right)).toEqual(indexes);
  });

  it("finds the row at a scroll offset", () => {
    const starts = [0, 56, 160, 264, 320];
    expect(rowAt(starts, 0)).toBe(0);
    expect(rowAt(starts, 55)).toBe(0);
    expect(rowAt(starts, 56)).toBe(1);
    expect(rowAt(starts, 200)).toBe(2);
    expect(rowAt(starts, 10_000)).toBe(4);
  });
});
