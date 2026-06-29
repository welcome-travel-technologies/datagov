/**
 * Canvas editor undo/redo. Uses the shared generic history stack
 * (`@/lib/history`) with a `HistorySnapshot` as the snapshot type — the
 * structural state (nodes, edges, groups, meta) serialized to JSON. The
 * viewport is intentionally excluded so panning/zooming is not undoable.
 *
 * Contract: call `push(snapshot())` BEFORE a mutating action (like the source's
 * `commit()`); `undo`/`redo` take the CURRENT snapshot and return the snapshot
 * to restore (or null when the stack is empty).
 */
import type { CanvasGroup, CanvasMeta, StoredEdge, StoredNode } from "@/lib/metrics-canvas/types";

export { useHistory } from "@/lib/history";

export interface HistorySnapshot {
  nodes: StoredNode[];
  edges: StoredEdge[];
  groups: CanvasGroup[];
  meta: CanvasMeta;
}
