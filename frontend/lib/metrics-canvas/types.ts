/**
 * Serialized document model for the visual Metrics Map (canvas) editor.
 *
 * A `CanvasDoc` is the JSON that round-trips to the backend (`MetricsMap.graph`),
 * to a downloaded `.json` file, and through undo/redo. It is intentionally a
 * plain, framework-agnostic shape: the React Flow live state (`Node`/`Edge`) is
 * converted to/from these `Stored*` types in `serialize.ts`.
 *
 * Parity note: this mirrors the source `State` object of the original
 * `metrics-map.html` (`meta` / `viewport` / `nodes` / `edges` / `groups` /
 * `drawings`). `drawings` is reserved for the v2 freehand brush — it is carried
 * through untouched so source-exported maps don't lose their strokes.
 */

/** Catalog provenance carried by a node dropped from the catalog palette. */
export interface CatalogRef {
  itemId: string;
  itemType: string; // PB_MEASURE | PB_COLUMN | PB_TABLE | PB_PAGE | …
  itemName: string;
  dataset?: string | null;
  table?: string | null;
  workspace?: string | null;
  datatype?: string | null;
  expression?: string | null;
  webUrl?: string | null;
}

/**
 * Per-node data. `elementType` is a key into the `TYPES` catalog
 * (measure/column/table/page/cylinder/section/text/sticky/…) and drives the
 * icon, color, and which React Flow component renders the node.
 */
export interface CanvasNodeData extends Record<string, unknown> {
  elementType: string;
  label: string;
  sub?: string;
  tooltip?: string;
  /** Optional overrides of the type's defaults. */
  borderColor?: string;
  fillColor?: string;
  fillAlpha?: number;
  textColor?: string;
  /** Per-element text-size multiplier (1 = follow the map-wide `fontScale`).
   *  The effective size is `meta.fontScale × data.fontScale`. */
  fontScale?: number;
  /** Catalog provenance when dropped from the catalog (was `pbipMeta`). */
  meta?: CatalogRef | null;
}

export interface CanvasEdgeData extends Record<string, unknown> {
  arrowStart?: boolean;
  arrowEnd?: boolean;
  dashed?: boolean;
  color?: string;
  /** Baked orthogonal route (absolute canvas points) produced by auto-arrange.
   *  When present the edge is drawn through these points; cleared on any manual
   *  move so the edge falls back to live floating attachment. */
  route?: { x: number; y: number }[];
}

export interface EdgeMarker {
  type: string;
  color?: string;
  width?: number;
  height?: number;
}

export interface StoredNode {
  id: string;
  /** React Flow component key: 'element' | 'shape' | 'container' | 'note'. */
  type: string;
  position: { x: number; y: number };
  width?: number | null;
  height?: number | null;
  data: CanvasNodeData;
}

export interface StoredEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string | null;
  targetHandle?: string | null;
  type?: string;
  label?: string;
  data?: CanvasEdgeData;
  markerStart?: EdgeMarker | null;
  markerEnd?: EdgeMarker | null;
  style?: Record<string, string | number> | null;
}

/** A named, colored grouping of nodes (rendered as a bbox overlay). */
export interface CanvasGroup {
  id: string;
  name: string;
  color: string;
  nodeIds: string[];
}

export interface CanvasMeta {
  name: string;
  description?: string;
  version: string;
  /** Map-wide text-size multiplier (1 = default). Applied to every node,
   *  edge label and group label so the whole map scales together. */
  fontScale?: number;
}

export interface CanvasViewport {
  x: number;
  y: number;
  zoom: number;
}

/** The full serialized canvas document. */
export interface CanvasDoc {
  meta: CanvasMeta;
  viewport: CanvasViewport;
  nodes: StoredNode[];
  edges: StoredEdge[];
  groups: CanvasGroup[];
  /** Reserved for the v2 freehand brush; round-tripped untouched. */
  drawings: unknown[];
}

export const CANVAS_DOC_VERSION = "1.0";

/** Bounds for the map-wide `fontScale` (see {@link CanvasMeta.fontScale}). */
export const DEFAULT_FONT_SCALE = 1;
export const FONT_SCALE_MIN = 0.6;
export const FONT_SCALE_MAX = 2;

/** Coerce an arbitrary value into a valid font scale, clamped to range. */
export function clampFontScale(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_FONT_SCALE;
  return Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, n));
}

/** An empty document for a fresh map. */
export function emptyDoc(name = "Untitled Map"): CanvasDoc {
  return {
    meta: { name, description: "", version: CANVAS_DOC_VERSION },
    viewport: { x: 0, y: 0, zoom: 1 },
    nodes: [],
    edges: [],
    groups: [],
    drawings: [],
  };
}
