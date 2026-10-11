/** Pure geometry for the photo stage: fit, zoom, pan bounds, swipes and double taps. */

export const MIN_SCALE = 1;
export const MAX_SCALE = 4;
export const DOUBLE_TAP_SCALE = 2.5;
export const DOUBLE_TAP_MS = 300;
export const DOUBLE_TAP_SLOP = 30;
export const TAP_SLOP = 10;

export type Size = {width: number; height: number};
export type Point = {x: number; y: number};
/** A zoom level and a pan offset from the stage center, in CSS pixels. */
export type View = {scale: number; x: number; y: number};
export type SwipeDirection = "next" | "previous";

export const IDENTITY_VIEW: View = {scale: 1, x: 0, y: 0};

/** The size of `content` scaled to fit inside `stage`, keeping its aspect ratio (never upscaled past the stage). */
export function fitWithin(content: Size | null, stage: Size): Size {
  if (!content || content.width <= 0 || content.height <= 0 || stage.width <= 0 || stage.height <= 0) {
    return {width: Math.max(0, stage.width), height: Math.max(0, stage.height)};
  }
  const ratio = Math.min(stage.width / content.width, stage.height / content.height);
  return {width: content.width * ratio, height: content.height * ratio};
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

/** Keep the zoomed picture covering the stage: the pan never reveals space beyond an edge. */
export function clampView(view: View, stage: Size, content: Size | null): View {
  const scale = clamp(view.scale, MIN_SCALE, MAX_SCALE);
  if (scale <= MIN_SCALE) return IDENTITY_VIEW;
  const fitted = fitWithin(content, stage);
  const maxX = Math.max(0, (fitted.width * scale - stage.width) / 2);
  const maxY = Math.max(0, (fitted.height * scale - stage.height) / 2);
  // `|| 0` folds negative zero, which would otherwise leak into transforms and comparisons.
  return {scale, x: clamp(view.x, -maxX, maxX) || 0, y: clamp(view.y, -maxY, maxY) || 0};
}

/**
 * Zoom to `scale` keeping the picture point under `focus` (relative to the
 * stage center) where it is, then clamp the pan.
 */
export function zoomAt(view: View, scale: number, focus: Point, stage: Size, content: Size | null): View {
  const target = clamp(scale, MIN_SCALE, MAX_SCALE);
  const ratio = target / view.scale;
  return clampView({
    scale: target,
    x: focus.x - (focus.x - view.x) * ratio,
    y: focus.y - (focus.y - view.y) * ratio,
  }, stage, content);
}

/** Double tap: zoom in around the tap, or back out when already zoomed. */
export function toggleZoom(view: View, focus: Point, stage: Size, content: Size | null): View {
  return view.scale > MIN_SCALE ? IDENTITY_VIEW : zoomAt(view, DOUBLE_TAP_SCALE, focus, stage, content);
}

/** A pinch from `start` as the finger distance changes and its midpoint moves (both relative to the stage center). */
export function pinchView(
  start: View,
  startDistance: number,
  distance: number,
  startMidpoint: Point,
  midpoint: Point,
  stage: Size,
  content: Size | null,
): View {
  if (startDistance <= 0) return start;
  const zoomed = zoomAt(start, start.scale * (distance / startDistance), startMidpoint, stage, content);
  return clampView({
    scale: zoomed.scale,
    x: zoomed.x + (midpoint.x - startMidpoint.x),
    y: zoomed.y + (midpoint.y - startMidpoint.y),
  }, stage, content);
}

/** The pan after a one-finger drag of a zoomed picture. */
export function panView(start: View, delta: Point, stage: Size, content: Size | null): View {
  return clampView({scale: start.scale, x: start.x + delta.x, y: start.y + delta.y}, stage, content);
}

/**
 * A horizontal swipe at scale 1: far enough (18% of the stage, at least 48
 * pixels) or a quick flick, and clearly more horizontal than vertical. A
 * leftward swipe shows the next item.
 */
export function swipeDirection(dx: number, dy: number, durationMs: number, stageWidth: number): SwipeDirection | null {
  const distance = Math.abs(dx);
  if (distance < Math.abs(dy) * 1.5) return null;
  const threshold = Math.max(48, stageWidth * 0.18);
  const flick = distance >= 24 && durationMs > 0 && distance / durationMs >= 0.5;
  if (distance < threshold && !flick) return null;
  return dx < 0 ? "next" : "previous";
}

export type Tap = {time: number; x: number; y: number};

/** Whether two taps make a double tap: close in time and place. */
export function isDoubleTap(previous: Tap | null, current: Tap): boolean {
  return previous !== null && current.time - previous.time <= DOUBLE_TAP_MS &&
    Math.hypot(current.x - previous.x, current.y - previous.y) <= DOUBLE_TAP_SLOP;
}

export function distanceBetween(first: Point, second: Point): number {
  return Math.hypot(second.x - first.x, second.y - first.y);
}

export function midpointOf(first: Point, second: Point): Point {
  return {x: (first.x + second.x) / 2, y: (first.y + second.y) / 2};
}
