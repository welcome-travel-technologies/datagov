/**
 * Layout engines for the lineage views.
 *
 *  - `layoutColumnCards`  — colibri left→right longest-path layering of model
 *    cards, columns stacked vertically inside each card (ports layoutColibriColumns).
 *  - `layoutAssetDagre`   — Dagre LR layout for the asset-level ego graph.
 *  - `layoutPathLR`       — BFS hop-distance LR layering for Track Back paths
 *    (ports initPathGraph).
 */
import dagre from "dagre";
import type { NetworkNode, NetworkLink } from "@/lib/api";
import type { ColumnModel, CardKind } from "@/lib/lineage/column-model";

export interface XY {
  x: number;
  y: number;
}

export const CARD_WIDTH = 232;
export const ROW_H = 26;
export const CARD_HEADER_H = 42;

/** Height of a card node given its column count. */
export function cardHeight(numCols: number): number {
  return CARD_HEADER_H + Math.max(1, numCols) * ROW_H + 8;
}

/**
 * Longest-path layering of model cards (level = x), cards stacked within a
 * level (y). Returns top-left positions keyed by card id.
 */
export function layoutColumnCards(
  model: ColumnModel,
  collapsed: Set<string> = new Set(),
  heights?: Record<string, number>,
): Record<string, XY> {
  // Horizontal gap between levels. A full PowerBI measure lineage is a long L→R
  // chain (sources → staging → … → measure → visual → page → report), so a wide
  // per-level pitch makes the whole graph thousands of px wide and the auto-fit
  // zooms out until the cards are unreadable. Keep the inter-card gap tight so
  // the chain stays compact and legible.
  const LEVEL_PITCH = CARD_WIDTH + 48;
  const COL_GAP = 44;
  // PowerBI "full lineage" ends in a deep report hierarchy (visual → page →
  // report) whose cards are header-only (~48px). At the normal COL_GAP the gap
  // dwarfs the card, so a level holding many visuals reads as a sparse,
  // far-apart column; pack consecutive header-only report cards tighter instead.
  const REPORT_GAP = 18;

  const cardIds = model.cards.map((c) => c.id);
  const kindOf: Record<string, CardKind> = {};
  for (const c of model.cards) kindOf[c.id] = c.cardKind;
  // Gap below card `aId` when `bId` is stacked under it in the same level.
  const gapBelow = (aId: string, bId: string) =>
    kindOf[aId] === "report" && kindOf[bId] === "report" ? REPORT_GAP : COL_GAP;
  // Callers that render a focused subset of columns (e.g. a collapsed card kept
  // open on its connected columns) pass explicit `heights`; otherwise derive the
  // height from the collapsed flag + full column count.
  const heightOf: Record<string, number> = {};
  for (const c of model.cards)
    heightOf[c.id] = heights?.[c.id] ?? (collapsed.has(c.id) ? CARD_HEADER_H : cardHeight(c.columns.length));

  // Model-level DAG from the column edges + report usage edges (card -> card),
  // so downstream report consumers lay out to the right of their producers.
  const adj: Record<string, Set<string>> = {};
  const indeg: Record<string, number> = {};
  for (const id of cardIds) {
    adj[id] = new Set();
    indeg[id] = 0;
  }
  const cardEdges = [...model.edges, ...(model.usageEdges ?? [])];
  for (const e of cardEdges) {
    const sp = e.source;
    const tp = e.target;
    if (sp && tp && sp !== tp && adj[sp] && adj[tp] && !adj[sp].has(tp)) {
      adj[sp].add(tp);
      indeg[tp] = (indeg[tp] || 0) + 1;
    }
  }

  // Longest-path layering (Kahn) -> level per card.
  const level: Record<string, number> = {};
  const queue: string[] = [];
  const indegCopy = { ...indeg };
  for (const id of cardIds) {
    if (!indeg[id]) {
      level[id] = 0;
      queue.push(id);
    }
  }
  while (queue.length) {
    const u = queue.shift()!;
    for (const v of adj[u]) {
      level[v] = Math.max(level[v] || 0, (level[u] || 0) + 1);
      if (--indegCopy[v] === 0) queue.push(v);
    }
  }
  for (const id of cardIds) if (level[id] === undefined) level[id] = 0;

  // Stack cards within each level.
  const byLevel: Record<number, string[]> = {};
  for (const id of cardIds) (byLevel[level[id]] ||= []).push(id);

  // Total stack height of each level (cards + the gaps between them) and the
  // tallest level overall, so every level can be vertically centred on a shared
  // midline below.
  const stackHeight: Record<number, number> = {};
  let maxStack = 0;
  for (const lvlKey of Object.keys(byLevel)) {
    const lvl = Number(lvlKey);
    const ids = byLevel[lvl];
    let h = 0;
    for (let i = 0; i < ids.length; i++) {
      h += heightOf[ids[i]];
      if (i < ids.length - 1) h += gapBelow(ids[i], ids[i + 1]);
    }
    stackHeight[lvl] = h;
    if (h > maxStack) maxStack = h;
  }

  // Vertically centre each level on the tallest level's midline. Without this
  // every level stacks from y=0, so a level holding one very tall card (e.g. a
  // 215-column Measures card) leaves short neighbouring levels pinned to the top
  // — fitView then zooms out over a huge empty gap and edges sweep diagonally
  // across it.
  const pos: Record<string, XY> = {};
  for (const lvlKey of Object.keys(byLevel)) {
    const lvl = Number(lvlKey);
    const ids = byLevel[lvl];
    let y = (maxStack - stackHeight[lvl]) / 2;
    for (let i = 0; i < ids.length; i++) {
      pos[ids[i]] = { x: lvl * LEVEL_PITCH, y };
      y += heightOf[ids[i]] + (i < ids.length - 1 ? gapBelow(ids[i], ids[i + 1]) : 0);
    }
  }
  return pos;
}

/** Dagre LR layout for the asset-level ego graph. */
export function layoutAssetDagre(
  nodes: NetworkNode[],
  links: NetworkLink[],
  sizeOf: (n: NetworkNode) => number,
): Record<string, XY> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 90, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));

  const ids = new Set(nodes.map((n) => n.id));
  for (const n of nodes) {
    const s = Math.max(40, sizeOf(n) + 14);
    g.setNode(n.id, { width: 120, height: s });
  }
  const seen = new Set<string>();
  for (const e of links) {
    if (e.source === e.target) continue;
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    const key = e.source + "->" + e.target;
    if (seen.has(key)) continue;
    seen.add(key);
    g.setEdge(e.source, e.target);
  }
  dagre.layout(g);

  const pos: Record<string, XY> = {};
  for (const id of ids) {
    const n = g.node(id);
    if (n) pos[id] = { x: n.x - n.width / 2, y: n.y - n.height / 2 };
  }
  return pos;
}

/**
 * BFS hop-distance LR layering from the `fromId` endpoint, staggering nodes
 * that share a level onto separate rows so parallel shortest paths fan out.
 */
export function layoutPathLR(
  nodes: NetworkNode[],
  links: NetworkLink[],
  fromId: string,
): Record<string, XY> {
  const X_PITCH = 230;
  const Y_PITCH = 92;
  const ids = new Set(nodes.map((n) => n.id));
  const adj: Record<string, string[]> = {};
  for (const id of ids) adj[id] = [];
  for (const e of links) {
    if (adj[e.source] && adj[e.target]) {
      adj[e.source].push(e.target);
      adj[e.target].push(e.source);
    }
  }
  const level: Record<string, number> = { [fromId]: 0 };
  const queue = [fromId];
  while (queue.length) {
    const u = queue.shift()!;
    for (const v of adj[u] || []) {
      if (level[v] === undefined) {
        level[v] = level[u] + 1;
        queue.push(v);
      }
    }
  }
  let maxLevel = 0;
  for (const k of Object.keys(level)) maxLevel = Math.max(maxLevel, level[k]);
  const rowCount: Record<number, number> = {};
  const pos: Record<string, XY> = {};
  for (const n of nodes) {
    const lvl = level[n.id] !== undefined ? level[n.id] : maxLevel + 1;
    const row = rowCount[lvl] || 0;
    rowCount[lvl] = row + 1;
    pos[n.id] = { x: lvl * X_PITCH, y: row * Y_PITCH };
  }
  return pos;
}
