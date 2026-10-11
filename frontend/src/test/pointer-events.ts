/**
 * jsdom has no PointerEvent, so `fireEvent.pointerDown(…, {clientX})` would
 * create a plain Event without coordinates. This MouseEvent subclass carries
 * the pointer fields React reads.
 */
export class TestPointerEvent extends MouseEvent {
  readonly pointerId: number;
  readonly pointerType: string;
  readonly isPrimary: boolean;

  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
    this.pointerType = init.pointerType ?? "touch";
    this.isPrimary = init.isPrimary ?? true;
  }
}

/** Install `TestPointerEvent` as `window.PointerEvent`; returns a function restoring the original. */
export function installPointerEvents(): () => void {
  const original = Object.getOwnPropertyDescriptor(window, "PointerEvent");
  Object.defineProperty(window, "PointerEvent", {configurable: true, writable: true, value: TestPointerEvent});
  return () => {
    if (original) Object.defineProperty(window, "PointerEvent", original);
    else delete (window as unknown as Record<string, unknown>).PointerEvent;
  };
}
