/**
 * Builds the colibri-style column-mode model from the raw network payload:
 * model/table "cards" containing stacked column rows, column→column lineage
 * edges (with cross-tool bridge detection), a column→card index, and the
 * column-level adjacency used to highlight a clicked column's full lineage.
 *
 * Ported from `buildColumnElements` / `highlightColumnLineage` in graph.html.
 */
import type { NetworkNode, NetworkLink } from "@/lib/api";
import { cleanLabel, isMemberGroup, memberGlyph, synthTitle, isBridge } from "@/lib/lineage/graph-utils";
import {
  detectModelType,
  aggregateLineageType,
  normalizeLineageType,
  type ModelType,
  type LineageType,
} from "@/lib/lineage/colibri";

export interface ColumnRow {
  id: string;
  label: string;
  glyph: string;
  group: string;
  datatype?: string | null;
  isMeasure: boolean;
  /** colibri-style lineage classification, derived structurally from edges. */
  lineageType: LineageType;
}

/** What a card represents: a real model/table, a single standalone measure
 *  (one card per measure), or a downstream PowerBI report/page/visual consumer. */
export type CardKind = "model" | "measures" | "report";

export interface ModelCard {
  id: string;
  label: string;
  group: string;
  isCenter: boolean;
  /** colibri model-type from the name prefix (dim_/fact_/int_/stg_). */
  modelType: ModelType;
  /** Distinguishes single-measure cards and report cards from real models. */
  cardKind: CardKind;
  /** Dataset this card belongs to (a measure card's dataset, else the table's). */
  datasetId: string | null;
  /** dbt tags / PowerBI labels on the container (for the Tags filter). */
  tags: string[];
  columns: ColumnRow[];
}

function nodeTags(n: { tags?: unknown }): string[] {
  return Array.isArray(n.tags) ? (n.tags as string[]) : [];
}

/** Data lineage vs. structural (FK/PK join, or WHERE/HAVING filter) edge. */
export type ColumnEdgeKind = "column" | "join" | "filter";

export interface ColumnEdge {
  id: string;
  source: string; // source card id
  sourceHandle: string; // source column id
  target: string; // target card id
  targetHandle: string; // target column id
  bridge: boolean;
  /** "column" = data lineage; "join"/"filter" = structural relationship. */
  kind: ColumnEdgeKind;
}

const STRUCTURAL_KINDS: ReadonlySet<string> = new Set(["join", "filter"]);

/** Suffix for the extra right-side target handle each column row exposes. Used
 *  only by intra-card (self-dependent) edges — e.g. a measure depending on
 *  another measure in the same Measures card — so they loop tightly on the right
 *  instead of wrapping all the way around to the left-side target handle. */
export const SELF_EDGE_TARGET_SUFFIX = "::loopin";

/** A downstream usage edge into the PowerBI report hierarchy. `sourceHandle` is
 *  set when the producer is a column/measure row (member → visual); for
 *  visual → page → report the producer is the card itself (no handle). */
export interface UsageEdge {
  id: string;
  source: string; // producer card id
  sourceHandle?: string; // producer column id, when the producer is a member row
  target: string; // consumer report/page/visual card id
}

const REPORT_GROUPS: ReadonlySet<string> = new Set(["PB_VISUAL", "PB_PAGE", "PB_REPORT"]);

export interface ColumnModel {
  cards: ModelCard[];
  edges: ColumnEdge[];
  /** column id -> the card it lives in */
  colToCard: Record<string, string>;
  /** column id -> downstream column ids */
  adjForward: Record<string, string[]>;
  /** column id -> upstream column ids */
  adjReverse: Record<string, string[]>;
  /** the centered column id, if the center node is a column/measure */
  centerColId: string | null;
  /** downstream report-hierarchy usage edges (unified mode); empty otherwise. */
  usageEdges?: UsageEdge[];
}

/** Synthetic id prefix for a single-measure card (one card per PB_MEASURE). */
const MEASURE_CARD_PREFIX = "__measure__::";

export interface BuildColumnModelOptions {
  /** Promote every PB_MEASURE to its own standalone card (one card per measure)
   *  instead of leaving it nested in its PowerBI home table. The measure stays a
   *  single row inside its own card, so column-lineage edges and the
   *  click-to-trace highlight keep attaching at the measure level. */
  measuresAsOwnCards?: boolean;
  /** Surface downstream PowerBI report consumers (visual/page/report) as cards,
   *  connected by their model-level usage edges. */
  includeReportCards?: boolean;
}

export function buildColumnModel(
  nodes: NetworkNode[],
  edges: NetworkLink[],
  centerId: string | null,
  opts: BuildColumnModelOptions = {},
): ColumnModel {
  // Map each member -> its container, from 'contains' edges.
  const parentOf: Record<string, string> = {};
  for (const e of edges) {
    if (e.kind === "contains") parentOf[e.target] = e.source;
  }

  const byId: Record<string, NetworkNode> = {};
  for (const n of nodes) byId[n.id] = n;

  // Optionally promote each measure to its own standalone card (one card per
  // measure) so every measure reads as its own asset, rather than nesting in its
  // PowerBI home table. Each measure stays a single row inside its own card so
  // column-lineage edges and the trace highlight still attach at the measure level.
  if (opts.measuresAsOwnCards) {
    for (const n of nodes) {
      if (n.group !== "PB_MEASURE") continue;
      const sid = MEASURE_CARD_PREFIX + n.id;
      if (!byId[sid]) {
        byId[sid] = {
          id: sid,
          group: "PB_MEASURE",
          label: n.label || n.id,
          dataset: (n.dataset as string) ?? null,
        } as NetworkNode;
      }
      parentOf[n.id] = sid; // override the home-table containment
    }
  }

  // Members without a real container get a synthetic group card by type.
  for (const n of nodes) {
    if (!isMemberGroup(n.group)) continue;
    const p = parentOf[n.id];
    if (p && p !== n.id && byId[p]) continue;
    const g = n.group || "UNKNOWN";
    const sid = "__grp__" + g;
    if (!byId[sid]) {
      byId[sid] = { id: sid, group: g, label: synthTitle(g) } as NetworkNode;
    }
    parentOf[n.id] = sid;
  }

  // Members per container.
  const childIds: Record<string, string[]> = {};
  for (const n of nodes) {
    if (!isMemberGroup(n.group)) continue;
    const p = parentOf[n.id];
    if (p && p !== n.id && byId[p]) (childIds[p] ||= []).push(n.id);
  }

  const colToCard: Record<string, string> = {};
  const cards: ModelCard[] = [];
  for (const cardId of Object.keys(childIds)) {
    const cn = byId[cardId];
    if (!cn) continue;
    const columns: ColumnRow[] = childIds[cardId].map((cid) => {
      const m = byId[cid];
      colToCard[cid] = cardId;
      return {
        id: cid,
        label: cleanLabel(m.label || m.id),
        glyph: memberGlyph(m),
        group: m.group || "UNKNOWN",
        datatype: m.datatype ?? null,
        isMeasure: m.group === "PB_MEASURE",
        // Prefer the real lineage type the flow engine computed (carried on the
        // node); fall back to the structural estimate once edges are known.
        lineageType: (normalizeLineageType(m.lineageType) ?? "unknown") as ColumnRow["lineageType"],
      };
    });
    const isMeasuresCard = cardId.startsWith(MEASURE_CARD_PREFIX);
    cards.push({
      id: cardId,
      label: cleanLabel(cn.label || cn.id),
      group: cn.group || "UNKNOWN",
      isCenter: cardId === centerId,
      modelType: detectModelType(cn.label || cn.id),
      cardKind: isMeasuresCard ? "measures" : "model",
      datasetId: (cn.dataset as string) ?? null,
      tags: nodeTags(cn),
      columns,
    });
  }

  // Always render the focused container, even with no (loaded) columns — so
  // selecting a single element shows its card immediately (depth-0 focus), and a
  // model whose columns weren't parsed still appears instead of a blank canvas.
  if (
    centerId &&
    byId[centerId] &&
    !childIds[centerId] &&
    !isMemberGroup(byId[centerId].group) &&
    !REPORT_GROUPS.has((byId[centerId].group || "").toUpperCase())
  ) {
    const cn = byId[centerId];
    cards.push({
      id: centerId,
      label: cleanLabel(cn.label || cn.id),
      group: cn.group || "UNKNOWN",
      isCenter: true,
      modelType: detectModelType(cn.label || cn.id),
      cardKind: "model",
      datasetId: (cn.dataset as string) ?? null,
      tags: nodeTags(cn),
      columns: [],
    });
  }

  // Column lineage + structural (join/filter) edges; endpoints must be placed
  // columns. Only data ("column") edges feed the lineage adjacency used for
  // lineage-type classification and the click-to-trace highlight; structural
  // edges are rendered but excluded from the trace.
  const placed = new Set(Object.keys(colToCard));
  const seen = new Set<string>();
  const colEdges: ColumnEdge[] = [];
  const adjForward: Record<string, string[]> = {};
  const adjReverse: Record<string, string[]> = {};
  for (const e of edges) {
    const kind: ColumnEdgeKind | null =
      e.kind === "column" ? "column" : STRUCTURAL_KINDS.has(e.kind ?? "") ? (e.kind as ColumnEdgeKind) : null;
    if (!kind) continue;
    if (e.source === e.target) continue;
    if (!placed.has(e.source) || !placed.has(e.target)) continue;
    const key = kind + ":" + e.source + "->" + e.target;
    if (seen.has(key)) continue;
    seen.add(key);
    colEdges.push({
      id: "e_" + key,
      source: colToCard[e.source],
      sourceHandle: e.source,
      target: colToCard[e.target],
      targetHandle: e.target,
      bridge: isBridge(e.source, e.target),
      kind,
    });
    if (kind === "column") {
      (adjForward[e.source] ||= []).push(e.target);
      (adjReverse[e.target] ||= []).push(e.source);
    }
  }

  // Lineage-type pass: keep the real value the flow engine provided; only for
  // columns the engine left unresolved do we estimate structurally from how many
  // upstream columns feed the column (now that adjReverse is fully populated).
  for (const card of cards) {
    for (const col of card.columns) {
      if (normalizeLineageType(col.lineageType)) continue; // real value from the API
      col.lineageType = aggregateLineageType((adjReverse[col.id] || []).length);
    }
  }

  const centerColId = centerId && placed.has(centerId) ? centerId : null;

  // Downstream PowerBI report hierarchy (unified mode): render visual/page/report
  // nodes as header-only consumer cards, wired by their model-level usage edges
  // (member → visual → page → report).
  const usageEdges: UsageEdge[] = [];
  if (opts.includeReportCards) {
    const reportNodeIds = new Set<string>();
    for (const n of nodes) {
      if (!REPORT_GROUPS.has((n.group || "").toUpperCase())) continue;
      reportNodeIds.add(n.id);
      cards.push({
        id: n.id,
        label: cleanLabel(n.label || n.id),
        group: n.group || "UNKNOWN",
        isCenter: n.id === centerId,
        modelType: "unknown",
        cardKind: "report",
        datasetId: null,
        tags: nodeTags(n),
        columns: [],
      });
    }
    const seenU = new Set<string>();
    for (const e of edges) {
      if (e.kind !== "model" || !reportNodeIds.has(e.target)) continue;
      let sourceCard: string | undefined;
      let sourceHandle: string | undefined;
      if (colToCard[e.source]) {
        sourceCard = colToCard[e.source];
        sourceHandle = e.source;
      } else if (reportNodeIds.has(e.source)) {
        sourceCard = e.source;
      }
      if (!sourceCard) continue;
      const key = `${sourceCard}|${sourceHandle ?? ""}->${e.target}`;
      if (seenU.has(key)) continue;
      seenU.add(key);
      usageEdges.push({ id: "u_" + key, source: sourceCard, sourceHandle, target: e.target });
    }
  }

  return { cards, edges: colEdges, colToCard, adjForward, adjReverse, centerColId, usageEdges };
}

/**
 * Full upstream + downstream column lineage for a clicked column. Returns the
 * set of active column ids and the active edge ids (both endpoints active) —
 * everything else is dimmed. Mirrors Cytoscape's predecessors()/successors().
 */
export function columnLineage(
  model: ColumnModel,
  colId: string,
): { cols: Set<string>; edges: Set<string>; cards: Set<string> } {
  const cols = new Set<string>([colId]);

  const walk = (start: string, adj: Record<string, string[]>) => {
    const stack = [start];
    while (stack.length) {
      const u = stack.pop()!;
      for (const v of adj[u] || []) {
        if (!cols.has(v)) {
          cols.add(v);
          stack.push(v);
        }
      }
    }
  };
  walk(colId, model.adjReverse); // upstream
  walk(colId, model.adjForward); // downstream

  const edges = new Set<string>();
  for (const e of model.edges) {
    if (cols.has(e.sourceHandle) && cols.has(e.targetHandle)) edges.add(e.id);
  }
  const cards = new Set<string>();
  for (const c of cols) {
    const card = model.colToCard[c];
    if (card) cards.add(card);
  }
  return { cols, edges, cards };
}

/**
 * colibri's "Copy dbt command for dependent models": build the model and
 * everything downstream of it (`+` suffix selector). Returns null for non-dbt
 * cards (PowerBI tables / Measures / report cards have no dbt selector).
 */
export function dbtBuildCommand(card: Pick<ModelCard, "label" | "group" | "cardKind">): string | null {
  if (card.cardKind !== "model" || !(card.group || "").startsWith("DBT_")) return null;
  return `dbt build -s ${card.label}+`;
}
