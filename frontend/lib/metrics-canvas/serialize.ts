/**
 * Conversions between the live React Flow state (`Node`/`Edge`) and the
 * serialized `CanvasDoc` that persists to the backend, downloads as `.json`,
 * and feeds undo/redo — plus JSON import/export and PNG export helpers.
 */
import type { Edge, Node } from "@xyflow/react";
import type {
  CanvasDoc,
  CanvasEdgeData,
  CanvasGroup,
  CanvasMeta,
  CanvasNodeData,
  CanvasViewport,
  EdgeMarker,
  StoredEdge,
  StoredNode,
} from "@/lib/metrics-canvas/types";
import { CANVAS_DOC_VERSION, clampFontScale, emptyDoc } from "@/lib/metrics-canvas/types";
import { hexToRgba } from "@/lib/metrics-canvas/shapes";

export type RfNode = Node<CanvasNodeData>;
export type RfEdge = Edge<CanvasEdgeData>;

interface SizedNode {
  width?: number | null;
  height?: number | null;
  style?: { width?: number | string; height?: number | string } | undefined;
  measured?: { width?: number; height?: number } | undefined;
}

function toNum(v: number | string | null | undefined): number | undefined {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const n = parseFloat(v);
    if (Number.isFinite(n)) return n;
  }
  return undefined;
}

/** Best-effort node size from explicit dims → style → measured. */
function readSize(n: SizedNode): { width?: number; height?: number } {
  return {
    width: toNum(n.width ?? undefined) ?? toNum(n.style?.width) ?? n.measured?.width,
    height: toNum(n.height ?? undefined) ?? toNum(n.style?.height) ?? n.measured?.height,
  };
}

// ---- nodes -----------------------------------------------------------------

export function rfToStoredNode(n: RfNode): StoredNode {
  const { width, height } = readSize(n as unknown as SizedNode);
  return {
    id: n.id,
    type: n.type ?? "element",
    position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
    width: width ?? null,
    height: height ?? null,
    data: n.data,
  };
}

export function storedToRfNode(s: StoredNode): RfNode {
  // RF resolves a node's size from `width`/`height` (NodeResizer writes back
  // here too), so we set those directly and avoid a redundant inline style.
  return {
    id: s.id,
    type: s.type || "element",
    position: { x: s.position?.x ?? 0, y: s.position?.y ?? 0 },
    data: s.data,
    // Section frames sit behind everything else.
    zIndex: s.type === "container" ? 0 : 1,
    ...(s.width != null ? { width: s.width } : {}),
    ...(s.height != null ? { height: s.height } : {}),
  };
}

// ---- edges -----------------------------------------------------------------

function cleanMarker(m: unknown): EdgeMarker | null {
  if (!m || typeof m !== "object") return null;
  const o = m as Record<string, unknown>;
  return {
    type: String(o.type ?? "arrowclosed"),
    color: typeof o.color === "string" ? o.color : undefined,
    width: typeof o.width === "number" ? o.width : undefined,
    height: typeof o.height === "number" ? o.height : undefined,
  };
}

export function rfToStoredEdge(e: RfEdge): StoredEdge {
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle ?? null,
    targetHandle: e.targetHandle ?? null,
    type: e.type,
    label: typeof e.label === "string" ? e.label : undefined,
    data: e.data,
    markerStart: cleanMarker(e.markerStart),
    markerEnd: cleanMarker(e.markerEnd),
    style: (e.style as Record<string, string | number> | undefined) ?? null,
  };
}

export function storedToRfEdge(s: StoredEdge): RfEdge {
  return {
    id: s.id,
    source: s.source,
    target: s.target,
    sourceHandle: s.sourceHandle ?? undefined,
    targetHandle: s.targetHandle ?? undefined,
    type: s.type || "smoothstep",
    label: s.label,
    data: s.data,
    markerStart: s.markerStart ? ({ ...s.markerStart, type: s.markerStart.type as never }) : undefined,
    markerEnd: s.markerEnd ? ({ ...s.markerEnd, type: s.markerEnd.type as never }) : undefined,
    style: s.style ?? undefined,
  };
}

/**
 * Give every labelled edge concrete inline fills for its label pill.
 *
 * React Flow renders an edge `label` as an SVG `<rect>` + `<text>` whose `fill`
 * comes from `--xy-edge-label-*` CSS variables defined on the `.react-flow`
 * root. PNG export (see {@link exportPng}) clones only the `.react-flow__viewport`
 * — a *descendant* of that root — so the variables are out of scope and the SVG
 * `fill` falls back to its initial value (black), turning each operator badge
 * (×, +, −, ÷ …) into a solid black box. Setting the fills inline here keeps the
 * variables out of the equation entirely: the pill stays a white chip with the
 * operator tinted to match its connector, in the editor and the exported image.
 *
 * Applied at the React Flow boundary only — it never touches the stored/exported
 * JSON (serialization reads the undecorated edge state).
 *
 * `fontScale` is the map-wide text multiplier; React Flow's default edge-label
 * size is 10px, so the scaled size is set inline here (captured verbatim by the
 * PNG export, same as the node text).
 */
export function decorateEdgeLabel(e: RfEdge, fontScale = 1): RfEdge {
  if (e.label == null || e.label === "") return e;
  const color = e.data?.color || (typeof e.style?.stroke === "string" ? e.style.stroke : "") || "#475569";
  const ring = hexToRgba(color, 0.4);
  return {
    ...e,
    labelShowBg: true,
    labelBgPadding: [6, 3],
    labelBgBorderRadius: 6,
    labelBgStyle: { fill: "#ffffff", ...(ring ? { stroke: ring, strokeWidth: 1 } : {}) },
    labelStyle: { fill: color, fontWeight: 700, fontSize: 10 * fontScale },
  };
}

// ---- document round-trip ---------------------------------------------------

export interface CanvasState {
  meta: CanvasMeta;
  viewport: CanvasViewport;
  nodes: RfNode[];
  edges: RfEdge[];
  groups: CanvasGroup[];
  drawings: unknown[];
}

export function toDoc(state: CanvasState): CanvasDoc {
  return {
    meta: { ...state.meta, version: CANVAS_DOC_VERSION },
    viewport: state.viewport,
    nodes: state.nodes.map(rfToStoredNode),
    edges: state.edges.map(rfToStoredEdge),
    groups: state.groups,
    drawings: state.drawings ?? [],
  };
}

export function fromDoc(doc: CanvasDoc): CanvasState {
  const d = normalizeDoc(doc);
  return {
    meta: d.meta,
    viewport: d.viewport,
    nodes: d.nodes.map(storedToRfNode),
    edges: d.edges.map(storedToRfEdge),
    groups: d.groups,
    drawings: d.drawings,
  };
}

/** Coerce an arbitrary parsed object into a valid CanvasDoc. */
export function normalizeDoc(raw: unknown): CanvasDoc {
  const base = emptyDoc();
  if (!raw || typeof raw !== "object") return base;
  const o = raw as Partial<CanvasDoc> & Record<string, unknown>;
  const meta = (o.meta && typeof o.meta === "object" ? o.meta : {}) as Partial<CanvasMeta>;
  const vp = (o.viewport && typeof o.viewport === "object" ? o.viewport : {}) as Partial<CanvasViewport>;
  return {
    meta: {
      name: typeof meta.name === "string" ? meta.name : base.meta.name,
      description: typeof meta.description === "string" ? meta.description : "",
      version: typeof meta.version === "string" ? meta.version : CANVAS_DOC_VERSION,
      fontScale: clampFontScale(meta.fontScale),
    },
    viewport: {
      x: typeof vp.x === "number" ? vp.x : 0,
      y: typeof vp.y === "number" ? vp.y : 0,
      zoom: typeof vp.zoom === "number" && vp.zoom > 0 ? vp.zoom : 1,
    },
    nodes: Array.isArray(o.nodes) ? (o.nodes as StoredNode[]).filter((n) => n && n.id) : [],
    edges: Array.isArray(o.edges) ? (o.edges as StoredEdge[]).filter((e) => e && e.id && e.source && e.target) : [],
    groups: Array.isArray(o.groups) ? (o.groups as CanvasGroup[]) : [],
    drawings: Array.isArray(o.drawings) ? o.drawings : [],
  };
}

// ---- JSON file import / export ---------------------------------------------

export function importJson(text: string): CanvasDoc {
  const parsed = JSON.parse(text);
  return normalizeDoc(parsed);
}

function downloadBlob(blob: Blob, fileName: string): void {
  if (typeof document === "undefined") return;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function safeFileName(name: string, ext: string): string {
  const base = (name || "metrics-map").trim().replace(/[^\w.-]+/g, "_").replace(/^_+|_+$/g, "") || "metrics-map";
  return `${base}.${ext}`;
}

export function exportJson(doc: CanvasDoc): void {
  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
  downloadBlob(blob, safeFileName(doc.meta.name, "json"));
}

/** PNG export of a DOM node (the React Flow viewport). Lazy-loads html-to-image.
 *  Pass `width`/`height`/`transform` (from `getViewportForBounds`) to capture the
 *  whole graph regardless of the current pan/zoom. */
export async function exportPng(
  el: HTMLElement,
  fileName: string,
  opts: { backgroundColor?: string; width?: number; height?: number; transform?: string } = {},
): Promise<void> {
  const { toPng } = await import("html-to-image");
  const style: Record<string, string> = {};
  if (opts.width) style.width = `${opts.width}px`;
  if (opts.height) style.height = `${opts.height}px`;
  if (opts.transform) style.transform = opts.transform;
  const dataUrl = await toPng(el, {
    backgroundColor: opts.backgroundColor ?? "#ffffff",
    pixelRatio: 2,
    width: opts.width,
    height: opts.height,
    style: Object.keys(style).length ? style : undefined,
    cacheBust: true,
    // Drop editor-only chrome so it can't bleed into the image: connection
    // ports and the selection resize frame/handles (NodeResizer).
    filter: (node) => {
      const cl = (node as Element).classList;
      return !cl || !(cl.contains("react-flow__handle") || cl.contains("react-flow__resize-control"));
    },
  });
  const res = await fetch(dataUrl);
  const blob = await res.blob();
  downloadBlob(blob, safeFileName(fileName, "png"));
}
