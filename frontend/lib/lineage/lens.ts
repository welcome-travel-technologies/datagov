/**
 * Card layering + the "lens" control. In colibri a card is tinted by its LAYER
 * (source / staging / intermediate / marts / …) regardless of lens, while the
 * bottom-bar **lens** decides what each column row's badge means (its legend is
 * shown bottom-right). Pure so it's unit-testable.
 *
 * Lenses (column badges):
 *   - lineage-type : pass-through / rename / transformation / unknown  (P/R/T/U)
 *   - datatype     : numeric / text / date / boolean                    (glyph)
 */
import type { ColumnRow, ModelCard } from "@/lib/lineage/column-model";
import {
  LINEAGE_TYPE_META,
  LINEAGE_TYPE_ORDER,
  type LineageType,
} from "@/lib/lineage/colibri";
import { typeGlyph } from "@/lib/lineage/graph-utils";

// ── card layers (colibri's left-to-right pipeline stages) ───────────────────
export type CardLayer =
  | "source"
  | "seed"
  | "staging"
  | "intermediate"
  | "marts"
  | "model"
  | "powerbi"
  | "measures"
  | "reports";

export const LAYER_LABEL: Record<CardLayer, string> = {
  source: "Sources",
  seed: "Seeds",
  staging: "Staging",
  intermediate: "Intermediate",
  marts: "Marts",
  model: "Models",
  powerbi: "Power BI",
  measures: "Measures",
  reports: "Reports",
};

const LAYER_COLOR: Record<CardLayer, string> = {
  source: "#f59e0b", // amber — raw sources
  seed: "#eab308", // yellow
  staging: "#60a5fa", // light blue
  intermediate: "#3b82f6", // blue
  marts: "#7c3aed", // purple — facts/dims
  model: "#3b82f6", // blue
  powerbi: "#0ea5e9", // sky
  measures: "#10b981", // green
  reports: "#f97316", // orange
};

/** Classify a card into a colibri pipeline layer (by resource type + name prefix). */
export function cardLayer(card: Pick<ModelCard, "group" | "label" | "cardKind">): CardLayer {
  if (card.cardKind === "measures") return "measures";
  if (card.cardKind === "report") return "reports";
  const g = (card.group || "").toUpperCase();
  if (g === "DBT_SOURCE") return "source";
  if (g === "DBT_SEED") return "seed";
  if (g === "PB_TABLE") return "powerbi";
  const name = String(card.label || "").toLowerCase();
  if (name.startsWith("stg_")) return "staging";
  if (name.startsWith("int_")) return "intermediate";
  if (/^(fct_|fact_|dim_|mart_|mrt_)/.test(name)) return "marts";
  return "model";
}

/** Accent color for a card header / border / tint (always layer-based). */
export function cardAccent(card: Pick<ModelCard, "group" | "label" | "cardKind">): string {
  return LAYER_COLOR[cardLayer(card)];
}

// ── lens (column badges) ────────────────────────────────────────────────────
export type LensId = "lineage-type" | "datatype";

export interface LensBadge {
  text: string;
  color: string;
}

export interface LensLegendItem {
  key: string;
  label: string;
  color: string;
}

export interface Lens {
  id: LensId;
  label: string;
  /** Badge shown at the right edge of a column row (null → none). */
  columnBadge(col: ColumnRow): LensBadge | null;
  legend: LensLegendItem[];
}

const LINEAGE_LETTER: Record<LineageType, string> = {
  "pass-through": "P",
  rename: "R",
  transformation: "T",
  unknown: "U",
};

type DataKind = "numeric" | "text" | "date" | "boolean" | "other";

const DATA_KIND_META: Record<DataKind, { label: string; color: string }> = {
  numeric: { label: "Numeric", color: "#2563eb" },
  text: { label: "Text", color: "#7c3aed" },
  date: { label: "Date / time", color: "#0891b2" },
  boolean: { label: "Boolean", color: "#f59e0b" },
  other: { label: "Other", color: "#94a3b8" },
};

export function dataKind(datatype?: string | null): DataKind {
  const t = (datatype || "").toLowerCase();
  if (!t) return "other";
  if (/bool/.test(t)) return "boolean";
  if (/(date|time|timestamp)/.test(t)) return "date";
  if (/(int|num|dec|float|double|real|money|serial|big)/.test(t)) return "numeric";
  if (/(char|text|string|uuid|json|byte|enum)/.test(t)) return "text";
  return "other";
}

export const LENSES: Record<LensId, Lens> = {
  "lineage-type": {
    id: "lineage-type",
    label: "Transformations",
    columnBadge: (col) => {
      const lt = (col.lineageType || "unknown") as LineageType;
      return { text: LINEAGE_LETTER[lt] ?? "U", color: LINEAGE_TYPE_META[lt].color };
    },
    legend: LINEAGE_TYPE_ORDER.map((lt) => ({
      key: lt,
      label: LINEAGE_TYPE_META[lt].label,
      color: LINEAGE_TYPE_META[lt].color,
    })),
  },
  datatype: {
    id: "datatype",
    label: "Data types",
    columnBadge: (col) => {
      if (col.isMeasure) return { text: "Σ", color: DATA_KIND_META.numeric.color };
      const k = dataKind(col.datatype);
      return { text: typeGlyph(col.datatype), color: DATA_KIND_META[k].color };
    },
    legend: (Object.keys(DATA_KIND_META) as DataKind[]).map((k) => ({
      key: k,
      label: DATA_KIND_META[k].label,
      color: DATA_KIND_META[k].color,
    })),
  },
};

export const LENS_ORDER: LensId[] = ["lineage-type", "datatype"];

export function getLens(id: LensId | string | null | undefined): Lens {
  return LENSES[(id as LensId) ?? "lineage-type"] ?? LENSES["lineage-type"];
}
