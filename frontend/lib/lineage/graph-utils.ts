/**
 * Pure graph helpers ported from the original Cytoscape `graph.html`. Kept
 * framework-agnostic and side-effect-free so they can be unit-tested and reused
 * by both the asset and column React-Flow views.
 */
import type { NetworkNode, NetworkLink, NodeGroup } from "@/lib/api";

/** Per-type node colors — identical palette to the legacy legend. */
export const GROUP_COLORS: Record<string, string> = {
  PB_TABLE: "#3b82f6",
  PB_MEASURE: "#10b981",
  PB_COLUMN: "#a855f7",
  PB_REPORT: "#f97316",
  PB_PAGE: "#fbbf24",
  PB_VISUAL: "#14b8a6",
  PB_FIELD: "#ec4899",
  DBT_MODEL: "#f97316",
  DBT_SOURCE: "#22c55e",
  DBT_SEED: "#eab308",
  DBT_TEST: "#94a3b8",
  DBT_COLUMN: "#a855f7",
  PB_WORKSPACE: "#6366f1",
  UNKNOWN: "#94a3b8",
  HUB: "#94a3b8",
};

export const GROUP_LABELS: Record<string, string> = {
  PB_TABLE: "PB Table",
  PB_MEASURE: "PB Measure",
  PB_COLUMN: "PB Column",
  PB_REPORT: "PB Report",
  PB_PAGE: "PB Page",
  PB_VISUAL: "PB Visual",
  PB_FIELD: "PB Field",
  DBT_MODEL: "dbt Model",
  DBT_SOURCE: "dbt Source",
  DBT_SEED: "dbt Seed",
  DBT_TEST: "dbt Test",
  DBT_COLUMN: "dbt Column",
};

/** Order used for grouping search results / start pickers. */
export const GROUP_ORDER = [
  "PB_REPORT", "PB_PAGE", "PB_VISUAL", "PB_TABLE", "PB_MEASURE", "PB_COLUMN", "PB_FIELD",
  "DBT_MODEL", "DBT_SOURCE", "DBT_SEED", "DBT_TEST", "DBT_COLUMN",
];

export function isDbtGroup(group?: string | null): boolean {
  return typeof group === "string" && group.indexOf("DBT_") === 0;
}
export function isPbGroup(group?: string | null): boolean {
  return typeof group === "string" && group.indexOf("PB_") === 0;
}
export function isColumnGroup(group?: string | null): boolean {
  return typeof group === "string" && group.indexOf("COLUMN") !== -1;
}
export function isMemberGroup(group?: string | null): boolean {
  return isColumnGroup(group) || group === "PB_MEASURE";
}
export function colorFor(group?: string | null): string {
  return (group && GROUP_COLORS[group]) || GROUP_COLORS.UNKNOWN;
}

/**
 * Strip ETL noise from a label so column-lineage rows read as plain business
 * names: a trailing disambiguation hash ("Name (c9f60527…)") and any raw
 * "TYPE::id" id that leaked through when a node had no metadata row.
 */
export function cleanLabel(s: string | null | undefined): string {
  if (s === null || s === undefined) return "";
  let out = String(s);
  const ci = out.indexOf("::");
  if (ci !== -1 && /^[A-Z_]+$/.test(out.slice(0, ci))) out = out.slice(ci + 2);
  out = out.replace(/\s*\(([0-9a-f]{8,}|[0-9a-f-]{12,})\)\s*$/i, "");
  return out.trim() || String(s);
}

/**
 * Display label for a node. Columns are prefixed with their parent (table or
 * model) so duplicates like "is_valid" across many tables can be told apart.
 */
export function nodeDisplayLabel(n: Pick<NetworkNode, "label" | "id" | "group" | "parent">): string {
  const base = cleanLabel(n.label || n.id);
  if (isColumnGroup(n.group) && n.parent) return cleanLabel(n.parent) + "." + base;
  return base;
}

/** colibri-style datatype glyph shown at the start of each column row. */
export function typeGlyph(dt?: string | null): string {
  const t = (dt || "").toString().toLowerCase();
  if (!t) return "•";
  if (/(char|text|string|uuid|json|byte|enum)/.test(t)) return "Aa";
  if (/(int|num|dec|float|double|real|money|serial|big)/.test(t)) return "#";
  if (/(date|time|timestamp)/.test(t)) return "◷";
  if (/bool/.test(t)) return "⊨";
  return "•";
}

/** Measures read as a measure (Σ); columns get a datatype glyph. */
export function memberGlyph(n: { group?: string | null; datatype?: string | null }): string {
  if (n && n.group === "PB_MEASURE") return "Σ";
  return typeGlyph(n ? n.datatype : null);
}

export function nodeTooltip(n: Pick<NetworkNode, "label" | "id" | "group" | "parent">): string {
  return nodeDisplayLabel(n) + "  (" + (n.group || "UNKNOWN") + ")";
}

/** Asset-mode edge classification (column vs cross-tool bridge). */
export function edgeClasses(e: NetworkLink): { column: boolean; bridge: boolean } {
  if (e.kind === "column") {
    const sPb = (e.source || "").indexOf("PB_") === 0;
    const tPb = (e.target || "").indexOf("PB_") === 0;
    return { column: true, bridge: sPb !== tPb };
  }
  return { column: false, bridge: false };
}

/** True when a column→column edge crosses tools (the dbt↔PowerBI bridge). */
export function isBridge(source: string, target: string): boolean {
  const sPb = source.indexOf("PB_") === 0;
  const tPb = target.indexOf("PB_") === 0;
  return sPb !== tPb;
}

export function synthTitle(g: NodeGroup): string {
  if (g === "PB_MEASURE") return "Measures";
  if (g === "PB_COLUMN") return "PowerBI columns";
  if (g === "DBT_COLUMN") return "dbt columns";
  return "Columns";
}

export function nodeSize(n: NetworkNode, centerId: string | null): number {
  if (n.id === centerId) return 34;
  if (n.group === "PB_FIELD") return 14;
  if (isColumnGroup(n.group)) return 18;
  return 26;
}
