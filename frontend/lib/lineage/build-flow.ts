/**
 * Converts the raw network payload into React-Flow node/edge arrays for each
 * view (asset / column / path), applying the legend/hub/per-node filters.
 * Pure (no React) so it can be unit-tested.
 */
import type { Node, Edge } from "@xyflow/react";
import type { NetworkNode, NetworkLink } from "@/lib/api";
import {
  colorFor,
  edgeClasses,
  nodeDisplayLabel,
  nodeSize,
} from "@/lib/lineage/graph-utils";
import {
  buildColumnModel,
  columnLineage,
  SELF_EDGE_TARGET_SUFFIX,
  type ColumnModel,
} from "@/lib/lineage/column-model";
import {
  layoutColumnCards,
  layoutAssetDagre,
  layoutPathLR,
  cardHeight,
  CARD_WIDTH,
  CARD_HEADER_H,
  type XY,
} from "@/lib/lineage/layout";
import type { CardKind } from "@/lib/lineage/column-model";
import { cardLayer } from "@/lib/lineage/lens";
import type { Highlight } from "@/components/lineage/canvas/context";

export const EDGE = {
  asset: "#cbd5e1",
  assetColumn: "#a855f7",
  assetBridge: "#0d9488",
  column: "#bfdbfe",
  columnBridge: "#5eead4",
  join: "#f59e0b", // structural FK→PK relationship
  filter: "#94a3b8", // structural WHERE/HAVING usage
  highlight: "#0d9488",
  path: "#0d9488",
};

/** Stroke colour for a column-mode edge that is NOT highlighted. */
function restingStroke(kind: string | undefined, bridge: boolean | undefined): string {
  if (kind === "join") return EDGE.join;
  if (kind === "filter") return EDGE.filter;
  return bridge ? EDGE.columnBridge : EDGE.column;
}

export interface Filters {
  hiddenGroups: Set<string>;
  hiddenNodeIds: Set<string>;
  hubThreshold: number | null;
}

// ---- column mode -----------------------------------------------------------

/**
 * "Show full lineage" pruning: keep only the columns that actually take part in
 * the lineage — an endpoint of some data / structural / usage edge, plus the
 * focused column. Drops the long tail of unconnected columns so a card like a
 * 215-column Measures table shrinks to the handful on the lineage. Report and
 * already-empty cards are left untouched. Mutates the freshly-built `model`.
 */
function pruneToLinkedColumns(model: ColumnModel): void {
  const linked = new Set<string>();
  for (const e of model.edges) {
    linked.add(e.sourceHandle);
    linked.add(e.targetHandle);
  }
  for (const u of model.usageEdges ?? []) if (u.sourceHandle) linked.add(u.sourceHandle);
  if (model.centerColId) linked.add(model.centerColId);
  for (const card of model.cards) {
    if (card.cardKind === "report" || card.columns.length === 0) continue;
    card.columns = card.columns.filter((c) => linked.has(c.id));
  }
}

function filterColumnModel(model: ColumnModel, hidden: Set<string>): ColumnModel {
  if (hidden.size === 0) return model;
  const keepCol = (group: string) => !hidden.has(group);
  const cards = model.cards
    .map((c) => ({ ...c, columns: c.columns.filter((col) => keepCol(col.group)) }))
    .filter((c) => c.columns.length > 0);
  const liveCols = new Set<string>();
  const colToCard: Record<string, string> = {};
  for (const c of cards)
    for (const col of c.columns) {
      liveCols.add(col.id);
      colToCard[col.id] = c.id;
    }
  const edges = model.edges.filter((e) => liveCols.has(e.sourceHandle) && liveCols.has(e.targetHandle));
  const adjForward: Record<string, string[]> = {};
  const adjReverse: Record<string, string[]> = {};
  for (const e of edges) {
    if (e.kind !== "column") continue; // structural edges aren't data lineage
    (adjForward[e.sourceHandle] ||= []).push(e.targetHandle);
    (adjReverse[e.targetHandle] ||= []).push(e.sourceHandle);
  }
  return {
    cards,
    edges,
    colToCard,
    adjForward,
    adjReverse,
    centerColId: model.centerColId && liveCols.has(model.centerColId) ? model.centerColId : null,
  };
}

export function buildColumnFlow(
  rawNodes: NetworkNode[],
  rawEdges: NetworkLink[],
  centerId: string | null,
  hiddenGroups: Set<string>,
  collapsed: Set<string> = new Set(),
): { nodes: Node[]; edges: Edge[]; model: ColumnModel } {
  const model = filterColumnModel(buildColumnModel(rawNodes, rawEdges, centerId), hiddenGroups);
  const pos = layoutColumnCards(model, collapsed);

  const nodes: Node[] = model.cards.map((card) => {
    const isCollapsed = collapsed.has(card.id);
    return {
      id: card.id,
      type: "modelCard",
      position: pos[card.id] ?? { x: 0, y: 0 },
      data: { card, collapsed: isCollapsed },
      style: { width: CARD_WIDTH, height: isCollapsed ? 42 : cardHeight(card.columns.length) },
      draggable: true,
    };
  });

  // When a card is collapsed its column handles disappear, so edges re-attach to
  // the card itself; many column edges can then collapse onto one card→card edge.
  const byKey = new Map<string, Edge>();
  for (const e of model.edges) {
    const sH = collapsed.has(e.source) ? undefined : e.sourceHandle;
    let tH = collapsed.has(e.target) ? undefined : e.targetHandle;
    // Intra-card self-dependent edge: loop on the right (see buildColibriFlow).
    if (e.source === e.target && typeof tH === "string") tH += SELF_EDGE_TARGET_SUFFIX;
    const structural = e.kind === "join" || e.kind === "filter";
    // Keep structural edges in their own lane so they don't merge with data edges.
    const key = `${e.kind}|${e.source}|${sH ?? ""}->${e.target}|${tH ?? ""}`;
    const existing = byKey.get(key);
    if (existing) {
      const d = existing.data as { bridge: boolean; modelEdgeIds: string[] };
      d.modelEdgeIds.push(e.id);
      d.bridge = d.bridge || e.bridge;
      continue;
    }
    byKey.set(key, {
      id:
        key === `column|${e.source}|${e.sourceHandle}->${e.target}|${e.targetHandle}` ||
        key === `column|${e.source}|${e.sourceHandle}->${e.target}|${e.targetHandle}${SELF_EDGE_TARGET_SUFFIX}`
          ? e.id
          : "agg_" + key,
      source: e.source,
      target: e.target,
      sourceHandle: sH,
      targetHandle: tH,
      type: e.source === e.target ? "selfLoop" : "default",
      data: { bridge: e.bridge, kind: e.kind, modelEdgeIds: [e.id] },
      style: {
        stroke: restingStroke(e.kind, e.bridge),
        strokeWidth: 1.5,
        // dotted for structural relationships, dashed for data lineage
        strokeDasharray: structural ? "1 4" : "4 3",
        opacity: structural ? 0.7 : 0.9,
      },
    });
  }

  return { nodes, edges: Array.from(byKey.values()), model };
}

// ---- colibri (unified column) mode -----------------------------------------

const CARD_NODE_TYPE: Record<CardKind, string> = {
  model: "modelCard",
  measures: "measuresCard",
  report: "reportCard",
};

export interface ColibriFlowOptions {
  collapsed?: Set<string>;
  hidden?: Set<string>;
  /** Manual drag overrides (card id -> top-left). Win over the computed layout. */
  positions?: Record<string, XY>;
  /** Layer keys to hide (colibri's Layers filter). */
  hiddenLayers?: Set<string>;
  /** When non-empty, show only cards carrying at least one of these tags. */
  tagsFilter?: Set<string>;
  /**
   * Pinned column whose lineage trace is active. When set, a *collapsed* card
   * keeps just the columns on that trace visible (instead of folding to a
   * header) so the connected path stays readable while everything unrelated
   * folds away. Unpinned / no-trace collapse stays header-only.
   */
  pinnedCol?: string | null;
  /** Include the downstream PowerBI report → page → visual consumer cards.
   *  Defaults to true to preserve behavior for callers that don't opt out. */
  includeReportCards?: boolean;
  /**
   * "Show full lineage" mode: keep only columns that participate in the lineage
   * (an endpoint of some edge) plus the focused column, dropping unconnected
   * columns so one giant card can't dominate the canvas. See pruneToLinkedColumns.
   */
  linkedColumnsOnly?: boolean;
}

/**
 * Build React-Flow elements for the colibri-style view: model/measures cards
 * with column rows, column→column lineage edges, each measure as its own card.
 * Node type is chosen by `cardKind` so the canvas can style measures/report
 * cards distinctly. Honors hidden cards and manual position overrides.
 */
export function buildColibriFlow(
  rawNodes: NetworkNode[],
  rawEdges: NetworkLink[],
  centerId: string | null,
  opts: ColibriFlowOptions = {},
): { nodes: Node[]; edges: Edge[]; model: ColumnModel } {
  const collapsed = opts.collapsed ?? new Set<string>();
  const hidden = opts.hidden ?? new Set<string>();
  const positions = opts.positions ?? {};
  const hiddenLayers = opts.hiddenLayers ?? new Set<string>();
  const tagsFilter = opts.tagsFilter ?? new Set<string>();
  const pinnedCol = opts.pinnedCol ?? null;
  const includeReportCards = opts.includeReportCards ?? true;

  const model = buildColumnModel(rawNodes, rawEdges, centerId, {
    measuresAsOwnCards: true,
    includeReportCards,
  });

  // "Show full lineage" keeps only the columns of interest (those on the lineage).
  if (opts.linkedColumnsOnly) pruneToLinkedColumns(model);

  const visibleCards = model.cards.filter((c) => {
    if (hidden.has(c.id)) return false;
    if (hiddenLayers.has(cardLayer(c))) return false;
    if (tagsFilter.size > 0 && !(c.tags ?? []).some((t) => tagsFilter.has(t))) return false;
    return true;
  });
  const visibleCardIds = new Set(visibleCards.map((c) => c.id));

  // Columns on the pinned column's lineage trace. A collapsed card keeps these
  // visible (focused collapse) instead of folding to a header, so collapsing
  // hides the unrelated columns while the connected ones stay put.
  const connectedCols = pinnedCol ? columnLineage(model, pinnedCol).cols : null;

  // For each collapsed card carrying ≥1 connected column, the subset of column
  // ids it keeps shown. Card ids absent here collapse to a header as before.
  const focusedColsByCard = new Map<string, Set<string>>();
  // Final rendered height per visible card (also drives the layout so taller
  // focused-collapse cards don't overlap their neighbours).
  const heights: Record<string, number> = {};
  for (const card of visibleCards) {
    const isCollapsed = collapsed.has(card.id);
    if (!isCollapsed) {
      heights[card.id] =
        card.cardKind === "report" ? CARD_HEADER_H + 6 : cardHeight(card.columns.length);
      continue;
    }
    const focused =
      connectedCols && card.cardKind !== "report"
        ? card.columns.filter((c) => connectedCols.has(c.id)).map((c) => c.id)
        : [];
    if (focused.length > 0) {
      focusedColsByCard.set(card.id, new Set(focused));
      heights[card.id] = cardHeight(focused.length);
    } else {
      heights[card.id] = CARD_HEADER_H; // header-only
    }
  }

  const pos = layoutColumnCards(model, collapsed, heights);

  const nodes: Node[] = visibleCards.map((card) => {
    const focused = focusedColsByCard.get(card.id);
    return {
      id: card.id,
      type: CARD_NODE_TYPE[card.cardKind] ?? "modelCard",
      position: positions[card.id] ?? pos[card.id] ?? { x: 0, y: 0 },
      data: {
        card,
        collapsed: collapsed.has(card.id),
        shownColIds: focused ? card.columns.filter((c) => focused.has(c.id)).map((c) => c.id) : undefined,
      },
      style: { width: CARD_WIDTH, height: heights[card.id] },
      draggable: true,
    };
  });

  // Resolve the handle a (collapsed-aware) edge endpoint attaches to:
  //  - expanded card                 → the column's own handle
  //  - focused-collapse, col shown   → the column's own handle
  //  - focused-collapse, col hidden  → null (drop: no card-level handle exists)
  //  - header-only collapse          → undefined (re-attach to the card itself)
  const handleFor = (cardId: string, colId: string): string | undefined | null => {
    if (!collapsed.has(cardId)) return colId;
    const focused = focusedColsByCard.get(cardId);
    if (focused) return focused.has(colId) ? colId : null;
    return undefined;
  };

  // Aggregate column edges (collapsed cards drop their handles → many column
  // edges collapse onto one card→card edge). Mirrors buildColumnFlow.
  const byKey = new Map<string, Edge>();
  for (const e of model.edges) {
    if (!visibleCardIds.has(e.source) || !visibleCardIds.has(e.target)) continue;
    const sH = handleFor(e.source, e.sourceHandle);
    let tH = handleFor(e.target, e.targetHandle);
    if (sH === null || tH === null) continue; // endpoint folded away in a focused collapse
    // Intra-card edge (a measure depending on another measure in the same card):
    // land on the row's right-side target handle so the edge loops tightly on the
    // right rather than wrapping all the way around to the left-side handle.
    if (e.source === e.target && typeof tH === "string") tH += SELF_EDGE_TARGET_SUFFIX;
    const structural = e.kind === "join" || e.kind === "filter";
    const key = `${e.kind}|${e.source}|${sH ?? ""}->${e.target}|${tH ?? ""}`;
    const existing = byKey.get(key);
    if (existing) {
      const d = existing.data as { bridge: boolean; modelEdgeIds: string[] };
      d.modelEdgeIds.push(e.id);
      d.bridge = d.bridge || e.bridge;
      continue;
    }
    byKey.set(key, {
      id:
        key === `column|${e.source}|${e.sourceHandle}->${e.target}|${e.targetHandle}` ||
        key === `column|${e.source}|${e.sourceHandle}->${e.target}|${e.targetHandle}${SELF_EDGE_TARGET_SUFFIX}`
          ? e.id
          : "agg_" + key,
      source: e.source,
      target: e.target,
      sourceHandle: sH,
      targetHandle: tH,
      type: e.source === e.target ? "selfLoop" : "default",
      data: { bridge: e.bridge, kind: e.kind, modelEdgeIds: [e.id] },
      style: {
        stroke: restingStroke(e.kind, e.bridge),
        strokeWidth: 1.5,
        strokeDasharray: structural ? "1 4" : "4 3",
        opacity: structural ? 0.7 : 0.9,
      },
    });
  }

  // Downstream report-usage edges (member/visual/page → consumer), solid gray.
  const edges = Array.from(byKey.values());
  for (const u of model.usageEdges ?? []) {
    if (!visibleCardIds.has(u.source) || !visibleCardIds.has(u.target)) continue;
    let sH: string | undefined;
    if (u.sourceHandle == null) {
      sH = undefined; // no source column → attaches to the card
    } else {
      const h = handleFor(u.source, u.sourceHandle);
      if (h === null) continue; // source column folded away in a focused collapse
      sH = h;
    }
    edges.push({
      id: u.id,
      source: u.source,
      target: u.target,
      sourceHandle: sH,
      type: "default",
      markerEnd: { type: "arrowclosed" as never, color: EDGE.asset, width: 12, height: 12 },
      data: { kind: "usage", modelEdgeIds: [u.id] },
      style: { stroke: EDGE.asset, strokeWidth: 1.5, opacity: 0.7 },
    });
  }

  return { nodes, edges, model };
}

// ---- asset mode ------------------------------------------------------------

export function buildAssetFlow(
  rawNodes: NetworkNode[],
  rawEdges: NetworkLink[],
  centerId: string | null,
  filters: Filters,
): { nodes: Node[]; edges: Edge[] } {
  const degree: Record<string, number> = {};
  for (const e of rawEdges) {
    if (e.source === e.target) continue;
    degree[e.source] = (degree[e.source] || 0) + 1;
    degree[e.target] = (degree[e.target] || 0) + 1;
  }

  const visible = rawNodes.filter((n) => {
    if (n.id === centerId) return true;
    if (filters.hiddenGroups.has(n.group)) return false;
    if (filters.hiddenNodeIds.has(n.id)) return false;
    return true;
  });
  const visibleIds = new Set(visible.map((n) => n.id));

  const visibleEdges = rawEdges.filter(
    (e) => e.source !== e.target && visibleIds.has(e.source) && visibleIds.has(e.target),
  );

  const pos = layoutAssetDagre(visible, visibleEdges, (n) => nodeSize(n, centerId));

  const threshold = filters.hubThreshold;
  const nodes: Node[] = visible.map((n) => {
    const isHub = threshold != null && threshold > 0 && n.id !== centerId && (degree[n.id] || 0) > threshold;
    return {
      id: n.id,
      type: isHub ? "hub" : "asset",
      position: pos[n.id] ?? { x: 0, y: 0 },
      data: {
        label: nodeDisplayLabel(n),
        group: n.group,
        isCenter: n.id === centerId,
        hubCount: isHub ? degree[n.id] : null,
      },
      draggable: true,
    };
  });

  const seen = new Set<string>();
  const edges: Edge[] = [];
  for (const e of visibleEdges) {
    const key = e.source + "->" + e.target;
    if (seen.has(key)) continue;
    seen.add(key);
    const cls = edgeClasses(e);
    const color = cls.bridge ? EDGE.assetBridge : cls.column ? EDGE.assetColumn : EDGE.asset;
    edges.push({
      id: "e_" + key,
      source: e.source,
      target: e.target,
      type: "default",
      markerEnd: { type: "arrowclosed" as never, color, width: 14, height: 14 },
      style: {
        stroke: color,
        strokeWidth: 1.5,
        strokeDasharray: cls.bridge ? "5 3" : undefined,
        opacity: 0.75,
      },
    });
  }

  return { nodes, edges };
}

// ---- path (track back) -----------------------------------------------------

export function buildPathFlow(
  rawNodes: NetworkNode[],
  rawEdges: NetworkLink[],
  fromId: string,
  toId: string,
): { nodes: Node[]; edges: Edge[] } {
  const pos = layoutPathLR(rawNodes, rawEdges, fromId);
  const nodes: Node[] = rawNodes.map((n) => ({
    id: n.id,
    type: "asset",
    position: pos[n.id] ?? { x: 0, y: 0 },
    data: {
      label: (n.id === fromId ? "▶ " : n.id === toId ? "◀ " : "") + nodeDisplayLabel(n),
      group: n.group,
      isCenter: n.id === fromId || n.id === toId,
    },
    draggable: true,
  }));

  const ids = new Set(rawNodes.map((n) => n.id));
  const seen = new Set<string>();
  const edges: Edge[] = [];
  for (const e of rawEdges) {
    if (e.source === e.target) continue;
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    const key = e.source + "->" + e.target;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({
      id: "e_" + key,
      source: e.source,
      target: e.target,
      type: "default",
      markerEnd: { type: "arrowclosed" as never, color: EDGE.path, width: 16, height: 16 },
      style: { stroke: EDGE.path, strokeWidth: 2.5, opacity: 0.95 },
    });
  }
  return { nodes, edges };
}

// ---- highlight (column mode) ----------------------------------------------

/** Re-style column-mode edges given the active lineage highlight. */
export function applyEdgeHighlight(edges: Edge[], highlight: Highlight): Edge[] {
  if (!highlight.active) return edges;
  return edges.map((e) => {
    const data = e.data as { bridge?: boolean; kind?: string; modelEdgeIds?: string[] } | undefined;
    const ids = data?.modelEdgeIds ?? [e.id];
    const on = ids.some((id) => highlight.edges.has(id));
    return {
      ...e,
      animated: on,
      style: {
        ...e.style,
        stroke: on ? EDGE.highlight : restingStroke(data?.kind, data?.bridge),
        strokeWidth: on ? 2.5 : 1.5,
        opacity: on ? 1 : 0.08,
      },
      zIndex: on ? 10 : 0,
    };
  });
}
