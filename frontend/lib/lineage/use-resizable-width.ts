"use client";

/**
 * Drag-to-resize width for a side panel, persisted to localStorage.
 *
 * Width lives in state (drives the panel) and a ref (read by the drag handler
 * without re-subscribing). Restored from / persisted to localStorage. Wire
 * `onResizeStart` to a handle's `onPointerDown` and `reset` to its
 * `onDoubleClick`.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

export interface ResizableWidthOptions {
  /** Initial width, and the value `reset` returns to. */
  defaultWidth: number;
  min: number;
  max: number;
  /** localStorage key — width is restored on mount and saved on drag end. */
  storageKey: string;
  /**
   * Which edge the drag handle sits on. "right" = handle on the panel's right
   * edge (e.g. left sidebar), so dragging right grows it. "left" = handle on
   * the left edge (e.g. right detail panel), so dragging left grows it.
   */
  edge: "left" | "right";
}

export interface ResizableWidth {
  width: number;
  onResizeStart: (e: ReactPointerEvent<HTMLElement>) => void;
  reset: () => void;
}

export function useResizableWidth({
  defaultWidth,
  min,
  max,
  storageKey,
  edge,
}: ResizableWidthOptions): ResizableWidth {
  const [width, setWidth] = useState(defaultWidth);
  const widthRef = useRef(defaultWidth);

  // Restore the persisted width once on mount.
  useEffect(() => {
    const saved = Number(localStorage.getItem(storageKey));
    if (saved >= min && saved <= max) {
      widthRef.current = saved;
      setWidth(saved);
    }
  }, [storageKey, min, max]);

  const persist = useCallback(
    (w: number) => {
      try {
        localStorage.setItem(storageKey, String(w));
      } catch {
        /* storage may be unavailable (private mode) — width still works for the session */
      }
    },
    [storageKey],
  );

  const onResizeStart = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = widthRef.current;
      const dir = edge === "left" ? -1 : 1; // a left-edge handle grows as the pointer moves left
      const onMove = (ev: PointerEvent) => {
        const w = Math.min(max, Math.max(min, startW + dir * (ev.clientX - startX)));
        widthRef.current = w;
        setWidth(w);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        persist(widthRef.current);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [edge, min, max, persist],
  );

  const reset = useCallback(() => {
    widthRef.current = defaultWidth;
    setWidth(defaultWidth);
    persist(defaultWidth);
  }, [defaultWidth, persist]);

  return { width, onResizeStart, reset };
}
