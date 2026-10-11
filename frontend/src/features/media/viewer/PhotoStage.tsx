import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type {MediaDetails} from "../types";
import {
  clampView,
  distanceBetween,
  IDENTITY_VIEW,
  isDoubleTap,
  MAX_SCALE,
  midpointOf,
  MIN_SCALE,
  panView,
  pinchView,
  swipeDirection,
  TAP_SLOP,
  toggleZoom,
  zoomAt,
  type Point,
  type Size,
  type SwipeDirection,
  type Tap,
  type View,
} from "./gestures";
import {useViewingDerivative} from "./useViewingDerivative";

export type PhotoStageProps = {
  entryId: string;
  details: MediaDetails | undefined;
  detailsFailed: boolean;
  refetchDetails(): Promise<unknown>;
  /** The summary's thumbnail, shown until a larger image loads. */
  thumbnailUrl: string | null;
  /** Display dimensions from the catalog, for pan bounds before the image loads. */
  dimensions: Size | null;
  /** Text alternative for the picture. */
  alt: string;
  onSwipe(direction: SwipeDirection): void;
};

type Gesture =
  | {type: "swipe"; pointerId: number; start: Point; time: number}
  | {type: "pan"; pointerId: number; start: Point; time: number; origin: View}
  | {type: "pinch"; startDistance: number; startMidpoint: Point; origin: View};

function stageSize(element: HTMLElement | null): Size {
  if (!element) return {width: 0, height: 0};
  return {
    width: element.clientWidth || element.offsetWidth,
    height: element.clientHeight || element.offsetHeight,
  };
}

/** A pointer position relative to the stage center. */
function fromCenter(element: HTMLElement | null, point: Point): Point {
  if (!element) return point;
  const rect = element.getBoundingClientRect();
  const size = stageSize(element);
  return {x: point.x - rect.left - size.width / 2, y: point.y - rect.top - size.height / 2};
}

const KEYBOARD_ZOOM_STEP = 1.5;
const KEYBOARD_PAN_STEP = 48;

const PHASE_TEXT: Partial<Record<string, string>> = {
  loading: "Loading photo…",
  preparing: "Preparing a larger preview…",
  failed: "A larger preview could not be created. Showing a smaller image.",
  stalled: "The larger preview is taking longer than expected. Showing a smaller image.",
  unsupported: "Preview isn't available for this file.",
  busy: "Busy, retrying…",
  backoff: "Couldn't prepare this file; try again later.",
  unavailable: "Your access to this file does not include viewing it.",
};

/**
 * The photo viewer stage: the preview derivative (requested once and polled
 * when missing, with the thumbnail shown meanwhile), the inline original as a
 * fallback when no preview can be made, and pointer gestures: swipe left or
 * right at normal size, pinch and double-tap zoom, and drag to pan within the
 * picture's bounds.
 */
export function PhotoStage({
  entryId,
  details,
  detailsFailed,
  refetchDetails,
  thumbnailUrl,
  dimensions,
  alt,
  onSwipe,
}: PhotoStageProps) {
  const stageRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const pointersRef = useRef(new Map<number, Point>());
  const gestureRef = useRef<Gesture | null>(null);
  const lastTapRef = useRef<Tap | null>(null);
  const [view, setView] = useState<View>(IDENTITY_VIEW);
  const [drag, setDrag] = useState(0);
  const [active, setActive] = useState(false);
  const [loadedUrl, setLoadedUrl] = useState<string | null>(null);
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const viewRef = useRef(view);
  const lastPointerTypeRef = useRef("mouse");
  const hintId = useId();

  useLayoutEffect(() => {
    viewRef.current = view;
  }, [view]);

  const preview = useViewingDerivative({
    entryId,
    kind: "preview",
    details,
    detailsFailed,
    refetchDetails,
    enabled: details === undefined || details.kind === "photo",
  });
  // A failed or slow preview may fall back to a browser-native original; an unsupported or
  // quarantined file never shows its original, only the thumbnail.
  const fallbackUrl = preview.phase === "failed" || preview.phase === "stalled"
    ? details?.original.inlineUrl ?? null
    : null;
  const downloadUrl = details?.permissions.export ? details.original.downloadUrl : null;
  const candidateUrl = preview.url ?? fallbackUrl;
  const mainUrl = candidateUrl !== null && candidateUrl !== failedUrl ? candidateUrl : null;
  const mainLoaded = mainUrl !== null && loadedUrl === mainUrl;
  const placeholderUrl = details?.derivatives.thumbnail?.url ?? thumbnailUrl;
  const loadFailed = candidateUrl !== null && candidateUrl === failedUrl;
  const statusText = loadFailed
    ? "The photo could not be loaded."
    : fallbackUrl !== null ? null : PHASE_TEXT[preview.phase] ?? null;

  const contentSize = (): Size | null => {
    const image = imageRef.current;
    if (image && image.naturalWidth > 0 && image.naturalHeight > 0) {
      return {width: image.naturalWidth, height: image.naturalHeight};
    }
    return dimensions;
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    lastPointerTypeRef.current = event.pointerType || "mouse";
    if (event.pointerType === "mouse" && event.button !== 0) return;
    // Controls over the picture (Retry) keep their own clicks; capture would retarget them.
    if (event.target instanceof Element && event.target.closest("button, a")) return;
    try {
      event.currentTarget.setPointerCapture?.(event.pointerId);
    } catch {
      // Capture is an enhancement; gestures work without it.
    }
    const point = {x: event.clientX, y: event.clientY};
    pointersRef.current.set(event.pointerId, point);
    setActive(true);
    const pointers = [...pointersRef.current.values()];
    if (pointers.length >= 2) {
      gestureRef.current = {
        type: "pinch",
        startDistance: distanceBetween(pointers[0]!, pointers[1]!),
        startMidpoint: fromCenter(stageRef.current, midpointOf(pointers[0]!, pointers[1]!)),
        origin: viewRef.current,
      };
      setDrag(0);
      return;
    }
    gestureRef.current = viewRef.current.scale > 1
      ? {type: "pan", pointerId: event.pointerId, start: point, time: event.timeStamp, origin: viewRef.current}
      : {type: "swipe", pointerId: event.pointerId, start: point, time: event.timeStamp};
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!pointersRef.current.has(event.pointerId)) return;
    const point = {x: event.clientX, y: event.clientY};
    pointersRef.current.set(event.pointerId, point);
    const gesture = gestureRef.current;
    if (!gesture) return;
    const stage = stageSize(stageRef.current);
    if (gesture.type === "pinch") {
      const pointers = [...pointersRef.current.values()];
      if (pointers.length < 2) return;
      setView(pinchView(
        gesture.origin,
        gesture.startDistance,
        distanceBetween(pointers[0]!, pointers[1]!),
        gesture.startMidpoint,
        fromCenter(stageRef.current, midpointOf(pointers[0]!, pointers[1]!)),
        stage,
        contentSize(),
      ));
    } else if (gesture.type === "pan" && gesture.pointerId === event.pointerId) {
      setView(panView(gesture.origin, {x: point.x - gesture.start.x, y: point.y - gesture.start.y}, stage, contentSize()));
    } else if (gesture.type === "swipe" && gesture.pointerId === event.pointerId) {
      const dx = point.x - gesture.start.x;
      const dy = point.y - gesture.start.y;
      setDrag(Math.abs(dx) > Math.abs(dy) ? dx : 0);
    }
  };

  const endPointer = (event: ReactPointerEvent<HTMLDivElement>, cancelled: boolean) => {
    if (!pointersRef.current.has(event.pointerId)) return;
    pointersRef.current.delete(event.pointerId);
    const gesture = gestureRef.current;
    setDrag(0);
    if (pointersRef.current.size === 0) setActive(false);
    if (!gesture) return;
    if (gesture.type === "pinch") {
      // Lifting one finger of a pinch continues as a pan of the zoomed picture.
      const [remainingId, remaining] = [...pointersRef.current.entries()][0] ?? [];
      gestureRef.current = remainingId !== undefined && remaining && viewRef.current.scale > 1
        ? {type: "pan", pointerId: remainingId, start: remaining, time: event.timeStamp, origin: viewRef.current}
        : null;
      return;
    }
    if (gesture.pointerId !== event.pointerId) return;
    gestureRef.current = null;
    if (cancelled) return;
    const dx = event.clientX - gesture.start.x;
    const dy = event.clientY - gesture.start.y;
    const duration = event.timeStamp - gesture.time;
    const stage = stageSize(stageRef.current);
    if (gesture.type === "swipe") {
      const direction = swipeDirection(dx, dy, duration, stage.width);
      if (direction) {
        lastTapRef.current = null;
        onSwipe(direction);
        return;
      }
    }
    // Mouse double clicks are handled by onDoubleClick; taps come from touch and pens.
    if (event.pointerType === "mouse" || Math.hypot(dx, dy) > TAP_SLOP) return;
    const tap = {time: event.timeStamp, x: event.clientX, y: event.clientY};
    if (isDoubleTap(lastTapRef.current, tap)) {
      lastTapRef.current = null;
      setView(toggleZoom(viewRef.current, fromCenter(stageRef.current, tap), stage, contentSize()));
    } else {
      lastTapRef.current = tap;
    }
  };

  /** Keyboard zoom: + and − zoom about the center, 0 resets, arrows pan a zoomed picture. */
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const current = viewRef.current;
    const stage = stageSize(stageRef.current);
    const center = {x: 0, y: 0};
    const pan: Partial<Record<string, Point>> = {
      ArrowLeft: {x: KEYBOARD_PAN_STEP, y: 0},
      ArrowRight: {x: -KEYBOARD_PAN_STEP, y: 0},
      ArrowUp: {x: 0, y: KEYBOARD_PAN_STEP},
      ArrowDown: {x: 0, y: -KEYBOARD_PAN_STEP},
    };
    let next: View | null = null;
    if (event.key === "+" || event.key === "=") {
      next = zoomAt(current, Math.min(MAX_SCALE, current.scale * KEYBOARD_ZOOM_STEP), center, stage, contentSize());
    } else if (event.key === "-" || event.key === "_") {
      next = zoomAt(current, Math.max(MIN_SCALE, current.scale / KEYBOARD_ZOOM_STEP), center, stage, contentSize());
    } else if (event.key === "0") {
      next = IDENTITY_VIEW;
    } else if (pan[event.key] && current.scale > 1) {
      // At normal size the arrows stay with the viewer, which moves between items.
      next = panView(current, pan[event.key]!, stage, contentSize());
    }
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    setView(next);
  };

  const frameStyle = {
    transform: `translate3d(${view.x + drag}px, ${view.y}px, 0) scale(${view.scale})`,
  };

  return (
    <div
      ref={stageRef}
      className="photo-stage"
      data-testid="photo-stage"
      data-scale={view.scale}
      data-zoomed={view.scale > 1 || undefined}
      role="group"
      aria-label="Photo"
      aria-describedby={hintId}
      aria-keyshortcuts="+ - 0"
      tabIndex={0}
      onKeyDown={handleKeyDown}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={(event) => endPointer(event, false)}
      onPointerCancel={(event) => endPointer(event, true)}
      onDoubleClick={(event) => {
        // Touch double taps are handled from pointer events; some browsers also report them as dblclick.
        if (lastPointerTypeRef.current !== "mouse") return;
        const stage = stageSize(stageRef.current);
        setView(toggleZoom(viewRef.current, fromCenter(stageRef.current, {x: event.clientX, y: event.clientY}),
          stage, contentSize()));
      }}
    >
      <div
        className="photo-stage__frame"
        data-dragging={drag !== 0 || undefined}
        data-active={active || undefined}
        style={frameStyle}
      >
        {placeholderUrl !== null && !mainLoaded ? (
          <img
            className="photo-stage__image photo-stage__image--placeholder"
            src={placeholderUrl}
            alt={mainUrl === null ? alt : ""}
            decoding="async"
            draggable={false}
          />
        ) : null}
        {mainUrl !== null ? (
          <img
            key={mainUrl}
            ref={imageRef}
            className="photo-stage__image"
            data-loaded={mainLoaded || undefined}
            src={mainUrl}
            alt={alt}
            decoding="async"
            draggable={false}
            onLoad={() => {
              setLoadedUrl(mainUrl);
              setView((current) => clampView(current, stageSize(stageRef.current), contentSize()));
            }}
            onError={() => setFailedUrl(mainUrl)}
          />
        ) : null}
      </div>
      <span id={hintId} className="visually-hidden">
        Plus and minus zoom, 0 resets, and arrow keys move a zoomed photo.
      </span>
      {statusText !== null ? (
        <div className="viewer-stage__status">
          <p role="status">{statusText}</p>
          {loadFailed ? (
            <button
              className="viewer-button interactive"
              type="button"
              onClick={() => {
                setFailedUrl(null);
                void refetchDetails();
              }}
            >
              Retry photo
            </button>
          ) : preview.phase === "failed" || preview.phase === "stalled" ? (
            <button className="viewer-button interactive" type="button" onClick={preview.retry}>
              Try the preview again
            </button>
          ) : preview.phase === "backoff" ? (
            <button
              className="viewer-button interactive"
              type="button"
              disabled={!preview.retryReady}
              onClick={preview.retry}
            >
              Try again
            </button>
          ) : preview.phase === "unsupported" && downloadUrl !== null ? (
            <a className="viewer-button interactive" href={downloadUrl} download>Download original</a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
