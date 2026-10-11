import {useEffect, useLayoutEffect, useRef, type KeyboardEvent} from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "video[controls]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function focusableWithin(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE)]
    .filter((element) => !element.closest("[hidden]") && element.getAttribute("aria-hidden") !== "true");
}

/** Keep Tab and Shift+Tab cycling inside `container`. */
export function trapTabKey(event: KeyboardEvent<HTMLElement>, container: HTMLElement | null): void {
  if (!container || event.key !== "Tab") return;
  const focusable = focusableWithin(container);
  if (focusable.length === 0) {
    event.preventDefault();
    container.focus({preventScroll: true});
    return;
  }
  const first = focusable[0]!;
  const last = focusable[focusable.length - 1]!;
  const active = document.activeElement;
  if (event.shiftKey && (active === first || active === container || !container.contains(active))) {
    event.preventDefault();
    last.focus({preventScroll: true});
  } else if (!event.shiftKey && (active === last || !container.contains(active))) {
    event.preventDefault();
    first.focus({preventScroll: true});
  }
}

/**
 * Modal behavior for the full-screen viewer, a native `<dialog>` opened with
 * `showModal()` (top layer, inert page behind it), as the filter panel does.
 * Where `showModal` is missing it falls back to the `open` attribute, and the
 * focus management below (initial focus on the dialog, focus pulled back if it
 * lands outside or on nothing, Tab trapping by the caller) keeps it modal for
 * keyboards. Focus is returned to the opener's tile or row by the opener page
 * itself, after the route change.
 */
export function useModalDialog<T extends HTMLElement>(resetKey: string) {
  const dialogRef = useRef<T>(null);

  useLayoutEffect(() => {
    const dialog = dialogRef.current as (T & Partial<Pick<HTMLDialogElement, "showModal" | "close" | "open">>) | null;
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    return () => {
      if (typeof dialog.close === "function") {
        if (dialog.open) dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    };
  }, []);

  useEffect(() => {
    dialogRef.current?.focus({preventScroll: true});
    const root = document.documentElement;
    root.classList.add("viewer-open");
    const onFocusIn = (event: FocusEvent) => {
      const dialog = dialogRef.current;
      if (dialog && event.target instanceof Node && !dialog.contains(event.target)) {
        dialog.focus({preventScroll: true});
      }
    };
    document.addEventListener("focusin", onFocusIn);
    return () => {
      document.removeEventListener("focusin", onFocusIn);
      root.classList.remove("viewer-open");
    };
  }, []);

  useEffect(() => {
    // A control that disappeared with the previous item (Next at the end) leaves focus on the body.
    const dialog = dialogRef.current;
    if (dialog && !dialog.contains(document.activeElement)) dialog.focus({preventScroll: true});
  }, [resetKey]);

  return dialogRef;
}
