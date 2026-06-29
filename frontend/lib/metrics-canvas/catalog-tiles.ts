/**
 * Pure mappers that turn catalog `Item`s into draggable palette tiles and turn
 * a dropped tile (or a static element type) into a canvas node. This is the
 * replacement for the source's "Load PBIP Metadata" upload: instead of parsing
 * an uploaded JSON, we feed the palette straight from the catalog API.
 *
 * Kept free of React / React Flow imports so it is unit-testable.
 */
import type { Item, RelationshipRef } from "@/lib/api";
import { defaultSize, rfTypeFor, typeMeta } from "@/lib/metrics-canvas/catalog";
import { uid } from "@/lib/metrics-canvas/ids";
import type { CanvasNodeData, CatalogRef, StoredNode } from "@/lib/metrics-canvas/types";

/** Catalog section keys shown in the palette. */
export type CatalogSection = "tables" | "measures" | "columns" | "pages" | "relationships";

/** The Power BI item_type fetched for each catalog section (relationships are derived). */
export const SECTION_ITEM_TYPE: Record<Exclude<CatalogSection, "relationships">, string> = {
  tables: "PB_TABLE",
  measures: "PB_MEASURE",
  columns: "PB_COLUMN",
  pages: "PB_PAGE",
};

/** A draggable palette tile (element-type or catalog-backed). */
export interface PaletteTile {
  /** Stable key for React lists. */
  key: string;
  /** TYPES catalog key driving icon/color/label + node component. */
  elementType: string;
  label: string;
  sub?: string;
  tooltip?: string;
  /** Catalog provenance (absent for static element tiles). */
  meta?: CatalogRef;
}

function num(v: number | null | undefined): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** Catalog provenance ref captured from an Item. */
export function itemToCatalogRef(it: Item): CatalogRef {
  return {
    itemId: it.item_id,
    itemType: String(it.item_type),
    itemName: it.item_name,
    dataset: it.dataset_name ?? null,
    table: it.table_name ?? null,
    workspace: it.workspace_name ?? null,
    datatype: it.datatype ?? null,
    expression: it.expression ?? null,
    webUrl: it.web_url ?? null,
  };
}

// ---- per-section Item → tile mappers --------------------------------------

export function tableTile(it: Item): PaletteTile {
  return {
    key: `t_${it.item_id}`,
    elementType: "table",
    label: it.item_name,
    sub: `${num(it.connected_measures)} m, ${num(it.connected_columns)} c`,
    tooltip: it.table_name || it.dataset_name || undefined,
    meta: itemToCatalogRef(it),
  };
}

export function measureTile(it: Item): PaletteTile {
  return {
    key: `m_${it.item_id}`,
    elementType: "measure",
    label: it.item_name,
    sub: it.table_name || it.dataset_name || undefined,
    tooltip: it.expression || undefined,
    meta: itemToCatalogRef(it),
  };
}

export function columnTile(it: Item): PaletteTile {
  const calc = (it.column_type || "").toLowerCase() === "calculated";
  return {
    key: `c_${it.item_id}`,
    elementType: calc ? "calc_column" : "column",
    label: it.item_name,
    sub: [it.table_name, it.datatype].filter(Boolean).join(" · ") || undefined,
    tooltip: it.expression || it.datatype || undefined,
    meta: itemToCatalogRef(it),
  };
}

export function pageTile(it: Item): PaletteTile {
  return {
    key: `p_${it.item_id}`,
    elementType: "page",
    label: it.item_name,
    tooltip: it.dataset_name || undefined,
    meta: itemToCatalogRef(it),
  };
}

/** Relationship tiles derived from a table/column item's `relationships_json`. */
export function relationshipTiles(it: Item): PaletteTile[] {
  const rels = (it.relationships_json ?? []) as RelationshipRef[];
  const fromTable = it.table_name || it.item_name;
  return rels.map((r, i) => {
    const toCol = [r.other_table, r.other_column].filter(Boolean).join(".");
    const label = `${fromTable} → ${toCol || "?"}`;
    return {
      key: `r_${it.item_id}_${i}`,
      elementType: "relationship",
      label,
      sub: [r.cardinality, r.other_cardinality].filter(Boolean).join(" → ") || undefined,
      tooltip: r.is_active === false ? "inactive relationship" : "active relationship",
      meta: itemToCatalogRef(it),
    };
  });
}

/** Map a page of items for a section into tiles. */
export function tilesForSection(section: CatalogSection, items: Item[]): PaletteTile[] {
  switch (section) {
    case "tables":
      return items.map(tableTile);
    case "measures":
      return items.map(measureTile);
    case "columns":
      return items.map(columnTile);
    case "pages":
      return items.map(pageTile);
    case "relationships":
      return items.flatMap(relationshipTiles);
  }
}

// ---- node construction -----------------------------------------------------

function baseData(elementType: string, label: string): CanvasNodeData {
  const meta = typeMeta(elementType);
  return {
    elementType,
    label: label || meta.label,
    borderColor: meta.color,
  };
}

/** A node created from a static element-type palette tile. */
export function makeTypeNode(elementType: string, position: { x: number; y: number }): StoredNode {
  const size = defaultSize(elementType);
  const label = elementType === "text" ? "Text" : typeMeta(elementType).label;
  return {
    id: uid("n"),
    type: rfTypeFor(elementType),
    position,
    width: size.width,
    height: size.height,
    data: baseData(elementType, label),
  };
}

/** A node created from a catalog-backed palette tile (carries its provenance). */
export function makeCatalogNode(tile: PaletteTile, position: { x: number; y: number }): StoredNode {
  const size = defaultSize(tile.elementType);
  return {
    id: uid("n"),
    type: rfTypeFor(tile.elementType),
    position,
    width: size.width,
    height: size.height,
    data: {
      ...baseData(tile.elementType, tile.label),
      sub: tile.sub,
      tooltip: tile.tooltip,
      meta: tile.meta ?? null,
    },
  };
}
