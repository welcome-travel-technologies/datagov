/**
 * Faithful TypeScript port of dbt-colibri's classification logic, adjusted to
 * our cross-tool (dbt ↔ PowerBI) network model.
 *
 * Ported from `dbt_colibri/report/generator.py`:
 *   - `detect_model_type()`            -> {@link detectModelType}
 *   - the per-target-column lineage-type aggregation in `build_full_lineage()`
 *     (a column with ≥2 upstream parents is a transformation; exactly one is a
 *     pass-through; none is unknown) -> {@link aggregateLineageType}
 *
 * Column lineage itself is now computed server-side by our vendored "flow"
 * engine (sqlglot on compiled dbt SQL) and delivered on the live API: each
 * column node carries a real `lineageType` and each column edge a real
 * `lineage_type`. The structural `aggregateLineageType` below remains only as a
 * fallback for columns the engine could not resolve (e.g. models without
 * compiled SQL).
 */

export type ModelType = "dimension" | "fact" | "intermediate" | "staging" | "unknown";

/** Derivation of a column relative to its upstream column(s). The first three
 *  come straight from the flow engine's SQL parsing; "unknown" marks an
 *  origin/source column (or one the engine could not resolve). */
export type LineageType = "pass-through" | "rename" | "transformation" | "unknown";

// Order matters: colibri checks these prefixes in turn (generator.py:101).
const MODEL_TYPE_PREFIX: ReadonlyArray<readonly [string, ModelType]> = [
  ["dim_", "dimension"],
  ["fact_", "fact"],
  ["int_", "intermediate"],
  ["stg_", "staging"],
];

/**
 * Port of colibri `detect_model_type`: classify a model by its name's prefix.
 * colibri uses `node_id.split('.')[-1]`; our card labels are already the model
 * name (e.g. "stg__driver_recruitment_goals"), so we take the last dotted
 * segment for safety and match the same prefixes. Double underscores
 * ("stg__x") still satisfy `startsWith("stg_")`, matching colibri.
 */
export function detectModelType(nameOrId: string | null | undefined): ModelType {
  if (!nameOrId) return "unknown";
  const slug = String(nameOrId).split(".").pop()!.trim().toLowerCase();
  for (const [prefix, type] of MODEL_TYPE_PREFIX) {
    if (slug.startsWith(prefix)) return type;
  }
  return "unknown";
}

export const MODEL_TYPE_META: Record<ModelType, { badge: string; color: string; label: string }> = {
  dimension: { badge: "DIM", color: "#2563eb", label: "Dimension" },
  fact: { badge: "FACT", color: "#7c3aed", label: "Fact" },
  intermediate: { badge: "INT", color: "#0891b2", label: "Intermediate" },
  staging: { badge: "STG", color: "#64748b", label: "Staging" },
  unknown: { badge: "", color: "#94a3b8", label: "Model" },
};

export const LINEAGE_TYPE_META: Record<LineageType, { color: string; label: string }> = {
  "pass-through": { color: "#10b981", label: "Pass-through" },
  rename: { color: "#38bdf8", label: "Rename" },
  transformation: { color: "#f59e0b", label: "Transformation" },
  unknown: { color: "#94a3b8", label: "No upstream" },
};

/** Legend order (transformation is the headline signal). */
export const LINEAGE_TYPE_ORDER: LineageType[] = ["transformation", "rename", "pass-through", "unknown"];

/** Lineage types the flow engine emits as a real value (vs. "unknown"). */
const REAL_LINEAGE_TYPES: ReadonlySet<string> = new Set(["pass-through", "rename", "transformation"]);

/** Coerce an API-provided lineage type to a known {@link LineageType}, or null
 *  if it is missing/unrecognised so callers can fall back to the structural
 *  estimate. */
export function normalizeLineageType(value: unknown): LineageType | null {
  return typeof value === "string" && REAL_LINEAGE_TYPES.has(value) ? (value as LineageType) : null;
}

/**
 * Port of colibri's per-target-column lineage-type aggregation
 * (generator.py `build_full_lineage`, lines ~384-409): given how many upstream
 * columns feed a target column, classify the column.
 *   - ≥2 parents  -> "transformation" (the column combines multiple inputs)
 *   - exactly 1   -> "pass-through"   (colibri uses the sole parent edge's type;
 *                                      we lack SQL so default to pass-through)
 *   - 0 parents   -> "unknown"        (an origin/source column, no lineage in)
 */
export function aggregateLineageType(parentCount: number): LineageType {
  if (parentCount >= 2) return "transformation";
  if (parentCount === 1) return "pass-through";
  return "unknown";
}

/** Should a model-type badge be shown? Only for dbt models with a known type. */
export function showModelTypeBadge(group: string | null | undefined, type: ModelType): boolean {
  return type !== "unknown" && typeof group === "string" && group.indexOf("DBT_") === 0;
}
