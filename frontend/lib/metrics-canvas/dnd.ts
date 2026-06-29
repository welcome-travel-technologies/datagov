/** Drag-and-drop contract between the palette tiles and the canvas drop target. */
import type { PaletteTile } from "@/lib/metrics-canvas/catalog-tiles";

export const MM_MIME = "application/x-mm-tile";

export type DragPayload =
  | { kind: "type"; elementType: string }
  | { kind: "catalog"; tile: PaletteTile };

export function readPayload(dt: DataTransfer): DragPayload | null {
  const raw = dt.getData(MM_MIME);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as DragPayload;
  } catch {
    return null;
  }
}
