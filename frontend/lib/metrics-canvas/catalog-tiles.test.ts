import { describe, it, expect } from "vitest";
import type { Item } from "@/lib/api";
import {
  columnTile,
  makeCatalogNode,
  makeTypeNode,
  measureTile,
  relationshipTiles,
  tableTile,
  tilesForSection,
} from "@/lib/metrics-canvas/catalog-tiles";

function item(partial: Partial<Item>): Item {
  return { item_id: "id1", item_name: "X", item_type: "PB_MEASURE", ...partial } as Item;
}

describe("catalog tile mappers", () => {
  it("maps a measure to a measure tile with table sub + DAX tooltip", () => {
    const t = measureTile(item({ item_name: "Revenue", table_name: "Sales", expression: "SUM(x)" }));
    expect(t.elementType).toBe("measure");
    expect(t.label).toBe("Revenue");
    expect(t.sub).toBe("Sales");
    expect(t.tooltip).toBe("SUM(x)");
    expect(t.meta?.itemId).toBe("id1");
  });

  it("maps a table with measure/column counts", () => {
    const t = tableTile(item({ item_type: "PB_TABLE", item_name: "Sales", connected_measures: 3, connected_columns: 7 }));
    expect(t.elementType).toBe("table");
    expect(t.sub).toBe("3 m, 7 c");
  });

  it("uses calc_column type for calculated columns", () => {
    const calc = columnTile(item({ item_type: "PB_COLUMN", column_type: "calculated", table_name: "T", datatype: "Int64" }));
    expect(calc.elementType).toBe("calc_column");
    expect(calc.sub).toBe("T · Int64");
    const plain = columnTile(item({ item_type: "PB_COLUMN", column_type: "data", table_name: "T" }));
    expect(plain.elementType).toBe("column");
  });

  it("expands relationships_json into relationship tiles", () => {
    const tiles = relationshipTiles(
      item({
        item_type: "PB_TABLE",
        table_name: "FactSales",
        relationships_json: [
          { cardinality: "Many", other_cardinality: "One", other_table: "DimDate", other_column: "DateKey", is_active: true },
          { other_table: "DimProduct", other_column: "Key", is_active: false },
        ],
      }),
    );
    expect(tiles).toHaveLength(2);
    expect(tiles[0].label).toContain("FactSales → DimDate.DateKey");
    expect(tiles[1].tooltip).toBe("inactive relationship");
  });

  it("tilesForSection dispatches by section", () => {
    const tiles = tilesForSection("tables", [item({ item_type: "PB_TABLE", item_name: "A" })]);
    expect(tiles[0].elementType).toBe("table");
  });
});

describe("node construction", () => {
  it("makeTypeNode picks the right React Flow component type", () => {
    expect(makeTypeNode("measure", { x: 0, y: 0 }).type).toBe("element");
    expect(makeTypeNode("database", { x: 0, y: 0 }).type).toBe("shape"); // cylinder shape
    expect(makeTypeNode("section", { x: 0, y: 0 }).type).toBe("container");
    expect(makeTypeNode("sticky", { x: 0, y: 0 }).type).toBe("note");
  });

  it("makeCatalogNode carries provenance + position", () => {
    const tile = measureTile(item({ item_name: "R", table_name: "S" }));
    const node = makeCatalogNode(tile, { x: 10, y: 20 });
    expect(node.position).toEqual({ x: 10, y: 20 });
    expect(node.data.elementType).toBe("measure");
    expect(node.data.meta?.itemName).toBe("R");
    expect(node.width).toBeGreaterThan(0);
  });
});
