/**
 * Element-type catalog for the Metrics Map canvas — ported verbatim from the
 * source `metrics-map.html` `TYPES` table (lines 1243-1273). Each entry supplies
 * the palette label, glyph icon, accent color, sidebar category, and (for the
 * "Shapes" group) the SVG shape key consumed by `buildShapeSVG`.
 */

export type ShapeKey = "cylinder" | "document" | "cloud" | "diamond" | "ellipse" | "hexagon";

export interface TypeMeta {
  label: string;
  icon: string;
  color: string;
  cat: string;
  shape?: ShapeKey;
}

export const TYPES: Record<string, TypeMeta> = {
  measure: { label: "Measure", icon: "∑", color: "#0078D4", cat: "Data Model" },
  column: { label: "Column", icon: "▤", color: "#107C10", cat: "Data Model" },
  calc_column: { label: "Calculated Column", icon: "ƒ▤", color: "#2E8B57", cat: "Data Model" },
  table: { label: "Table", icon: "▦", color: "#5C2D91", cat: "Data Model" },
  dimension: { label: "Dimension", icon: "◇", color: "#4A4A4A", cat: "Data Model" },
  date_table: { label: "Date Table", icon: "🗓", color: "#004B1C", cat: "Data Model" },
  hierarchy: { label: "Hierarchy", icon: "⛢", color: "#6B2FA0", cat: "Data Model" },
  relationship: { label: "Relationship", icon: "⤳", color: "#1E1E1E", cat: "Data Model" },
  parameter: { label: "Parameter", icon: "⚙", color: "#FB6F92", cat: "Data Model" },
  data_source: { label: "Data Source", icon: "🛢", color: "#00B4D8", cat: "Data Model" },
  page: { label: "Report Page", icon: "▭", color: "#D83B01", cat: "Report" },
  tooltip_page: { label: "Tooltip Page", icon: "💬", color: "#E55B05", cat: "Report" },
  drillthrough: { label: "Drillthrough Page", icon: "⤓", color: "#B23A00", cat: "Report" },
  bookmark: { label: "Bookmark", icon: "🔖", color: "#A0522D", cat: "Report" },
  visual: { label: "Visual", icon: "📊", color: "#FFB900", cat: "Report" },
  slicer: { label: "Slicer", icon: "⟝", color: "#C19A00", cat: "Report" },
  report: { label: "Report", icon: "📑", color: "#FFB900", cat: "Delivery" },
  dashboard: { label: "Dashboard", icon: "▩", color: "#008272", cat: "Delivery" },
  kpi: { label: "KPI", icon: "◎", color: "#E81123", cat: "Delivery" },
  text: { label: "Text", icon: "𝐓", color: "#444444", cat: "Annotations" },
  section: { label: "Section", icon: "▢", color: "#a855f7", cat: "Annotations" },
  sticky: { label: "Sticky Note", icon: "✎", color: "#f59e0b", cat: "Annotations" },
  database: { label: "Database", icon: "🛢", color: "#16a34a", cat: "Shapes", shape: "cylinder" },
  document: { label: "Document", icon: "📄", color: "#475569", cat: "Shapes", shape: "document" },
  cloud: { label: "Cloud", icon: "☁", color: "#0ea5e9", cat: "Shapes", shape: "cloud" },
  diamond: { label: "Decision", icon: "◆", color: "#f97316", cat: "Shapes", shape: "diamond" },
  ellipse: { label: "Ellipse", icon: "⬭", color: "#7c3aed", cat: "Shapes", shape: "ellipse" },
  hexagon: { label: "Hexagon", icon: "⬡", color: "#0891b2", cat: "Shapes", shape: "hexagon" },
  custom: { label: "Custom", icon: "◯", color: "#777777", cat: "Other" },
};

export const TYPE_KEYS_ORDERED = Object.keys(TYPES);

export const CAT_ORDER = ["Data Model", "Report", "Delivery", "Annotations", "Shapes", "Other"];

const FALLBACK: TypeMeta = { label: "Custom", icon: "◯", color: "#777777", cat: "Other" };

/** Type metadata for an element-type key, falling back to "custom". */
export function typeMeta(elementType: string | undefined | null): TypeMeta {
  return (elementType && TYPES[elementType]) || FALLBACK;
}

/** Element types grouped by their sidebar category, in `CAT_ORDER`. */
export function typesByCategory(): { cat: string; keys: string[] }[] {
  return CAT_ORDER.map((cat) => ({
    cat,
    keys: TYPE_KEYS_ORDERED.filter((k) => TYPES[k].cat === cat),
  })).filter((g) => g.keys.length > 0);
}

/** Which React Flow node component renders a given element type. */
export function rfTypeFor(elementType: string): "element" | "shape" | "container" | "note" {
  const meta = TYPES[elementType];
  if (meta?.shape) return "shape";
  if (elementType === "section") return "container";
  if (elementType === "text" || elementType === "sticky") return "note";
  return "element";
}

/** Default node box size (w×h) for a freshly-created element type. */
export function defaultSize(elementType: string): { width: number; height: number } {
  if (elementType === "section") return { width: 320, height: 220 };
  if (elementType === "sticky") return { width: 180, height: 140 };
  if (elementType === "text") return { width: 160, height: 40 };
  if (TYPES[elementType]?.shape) return { width: 140, height: 96 };
  return { width: 190, height: 60 };
}
