/**
 * Saved Lineage Views — persists a snapshot of the current canvas (focused set,
 * layout, trace, lens, filters and the loaded graph) so it can be reopened.
 * v1 stores in localStorage; a server-backed store (MetricsMap-style) can mirror
 * this shape later.
 *
 * The list mutations (`upsertView` / `removeView`) are pure and unit-tested; the
 * localStorage read/write wrappers are thin and guarded for SSR / tests.
 */
import type { NetworkNode, NetworkLink } from "@/lib/api";
import type { XY } from "@/lib/lineage/layout";

export interface SavedView {
  id: string;
  name: string;
  createdAt: number;
  centerId: string | null;
  collapsed: string[];
  hidden: string[];
  positions: Record<string, XY>;
  layoutMode: string;
  pinnedCol: string | null;
  lens: string;
  layersFilter: string[];
  tagsFilter: string[];
  linkedOnly?: boolean;
  rawNodes: NetworkNode[];
  rawEdges: NetworkLink[];
  viewport?: { x: number; y: number; zoom: number } | null;
}

export const SAVED_VIEWS_KEY = "wdc.lineage.views.v1";

// ── pure list helpers ───────────────────────────────────────────────────────
/** Insert or replace `view` (matched by id), newest first. */
export function upsertView(views: SavedView[], view: SavedView): SavedView[] {
  const rest = views.filter((v) => v.id !== view.id);
  return [view, ...rest].sort((a, b) => b.createdAt - a.createdAt);
}

export function removeView(views: SavedView[], id: string): SavedView[] {
  return views.filter((v) => v.id !== id);
}

// ── localStorage IO (guarded) ───────────────────────────────────────────────
function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function loadViews(): SavedView[] {
  const s = storage();
  if (!s) return [];
  try {
    const raw = s.getItem(SAVED_VIEWS_KEY);
    const parsed = raw ? (JSON.parse(raw) as SavedView[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function persistViews(views: SavedView[]): void {
  const s = storage();
  if (!s) return;
  try {
    s.setItem(SAVED_VIEWS_KEY, JSON.stringify(views));
  } catch {
    /* quota / serialization failure — non-fatal */
  }
}

/** Convenience: load → upsert → persist, returning the new list. */
export function saveView(view: SavedView): SavedView[] {
  const next = upsertView(loadViews(), view);
  persistViews(next);
  return next;
}

/** Convenience: load → remove → persist, returning the new list. */
export function deleteView(id: string): SavedView[] {
  const next = removeView(loadViews(), id);
  persistViews(next);
  return next;
}
