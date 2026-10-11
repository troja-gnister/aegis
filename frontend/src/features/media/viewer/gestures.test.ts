import {describe, expect, it} from "vitest";
import {
  clampView,
  DOUBLE_TAP_SCALE,
  fitWithin,
  IDENTITY_VIEW,
  isDoubleTap,
  MAX_SCALE,
  panView,
  pinchView,
  swipeDirection,
  toggleZoom,
  zoomAt,
} from "./gestures";

const stage = {width: 400, height: 800};
const landscape = {width: 4000, height: 3000};

describe("photo stage geometry", () => {
  it("fits a picture inside the stage keeping its aspect ratio", () => {
    expect(fitWithin(landscape, stage)).toEqual({width: 400, height: 300});
    expect(fitWithin(null, stage)).toEqual(stage);
  });

  it("clamps the pan so a zoomed picture never reveals space beyond its edges", () => {
    // At 2x the 400x300 fit is 800x600: 200 px of horizontal slack each way, none vertically.
    expect(clampView({scale: 2, x: 999, y: 999}, stage, landscape)).toEqual({scale: 2, x: 200, y: 0});
    expect(clampView({scale: 2, x: -999, y: -50}, stage, landscape)).toEqual({scale: 2, x: -200, y: 0});
    expect(clampView({scale: 0.5, x: 30, y: 30}, stage, landscape)).toEqual(IDENTITY_VIEW);
    expect(clampView({scale: 10, x: 0, y: 0}, stage, landscape).scale).toBe(MAX_SCALE);
  });

  it("zooms around a focus point and pans within bounds", () => {
    const zoomed = zoomAt(IDENTITY_VIEW, 2, {x: 100, y: 0}, stage, landscape);
    expect(zoomed).toEqual({scale: 2, x: -100, y: 0});
    expect(panView(zoomed, {x: 500, y: 40}, stage, landscape)).toEqual({scale: 2, x: 200, y: 0});
  });

  it("toggles double-tap zoom in and back out", () => {
    const zoomed = toggleZoom(IDENTITY_VIEW, {x: 0, y: 0}, stage, landscape);
    expect(zoomed.scale).toBe(DOUBLE_TAP_SCALE);
    expect(toggleZoom(zoomed, {x: 0, y: 0}, stage, landscape)).toEqual(IDENTITY_VIEW);
  });

  it("scales a pinch by the finger distance ratio, within limits", () => {
    expect(pinchView(IDENTITY_VIEW, 100, 200, {x: 0, y: 0}, {x: 0, y: 0}, stage, landscape).scale).toBe(2);
    expect(pinchView(IDENTITY_VIEW, 100, 50, {x: 0, y: 0}, {x: 0, y: 0}, stage, landscape)).toEqual(IDENTITY_VIEW);
    expect(pinchView(IDENTITY_VIEW, 100, 1000, {x: 0, y: 0}, {x: 0, y: 0}, stage, landscape).scale).toBe(MAX_SCALE);
  });
});

describe("swipes and taps", () => {
  it.each([
    [-120, 0, 400, "next"],
    [120, 10, 400, "previous"],
    [-30, 0, 400, null],
    [-120, 100, 400, null],
    [-40, 0, 40, "next"],
    [-60, 0, 2000, null],
  ] as const)("reads dx=%d dy=%d over %d ms as %s", (dx, dy, duration, expected) => {
    expect(swipeDirection(dx, dy, duration, 400)).toBe(expected);
  });

  it("recognizes a double tap only when close in time and place", () => {
    expect(isDoubleTap(null, {time: 0, x: 0, y: 0})).toBe(false);
    expect(isDoubleTap({time: 0, x: 10, y: 10}, {time: 200, x: 20, y: 20})).toBe(true);
    expect(isDoubleTap({time: 0, x: 10, y: 10}, {time: 400, x: 10, y: 10})).toBe(false);
    expect(isDoubleTap({time: 0, x: 10, y: 10}, {time: 100, x: 100, y: 10})).toBe(false);
  });
});
