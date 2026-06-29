import { describe, it, expect } from "vitest";
import { getLens, dataKind, cardLayer, cardAccent, LENS_ORDER } from "@/lib/lineage/lens";
import type { ColumnRow, ModelCard } from "@/lib/lineage/column-model";

const col = (extra: Partial<ColumnRow> = {}): ColumnRow =>
  ({
    id: "c",
    label: "c",
    glyph: "#",
    group: "DBT_COLUMN",
    isMeasure: false,
    lineageType: "unknown",
    ...extra,
  }) as ColumnRow;

const card = (extra: Partial<ModelCard> = {}): ModelCard =>
  ({
    id: "m",
    label: "stg_orders",
    group: "DBT_MODEL",
    isCenter: false,
    modelType: "staging",
    cardKind: "model",
    datasetId: null,
    tags: [],
    columns: [],
    ...extra,
  }) as ModelCard;

describe("dataKind", () => {
  it("classifies common datatypes", () => {
    expect(dataKind("integer")).toBe("numeric");
    expect(dataKind("varchar(20)")).toBe("text");
    expect(dataKind("timestamp")).toBe("date");
    expect(dataKind("boolean")).toBe("boolean");
    expect(dataKind("")).toBe("other");
  });
});

describe("cardLayer / cardAccent", () => {
  it("maps resource types and name prefixes to colibri layers", () => {
    expect(cardLayer(card({ group: "DBT_SOURCE", label: "raw_orders" }))).toBe("source");
    expect(cardLayer(card({ group: "DBT_SEED", label: "country_codes" }))).toBe("seed");
    expect(cardLayer(card({ label: "stg_orders" }))).toBe("staging");
    expect(cardLayer(card({ label: "int_payments" }))).toBe("intermediate");
    expect(cardLayer(card({ label: "fct_payments" }))).toBe("marts");
    expect(cardLayer(card({ group: "PB_TABLE", label: "Sales" }))).toBe("powerbi");
    expect(cardLayer(card({ cardKind: "measures", label: "Measures" }))).toBe("measures");
    expect(cardLayer(card({ cardKind: "report", label: "Exec" }))).toBe("reports");
  });
  it("gives sources and models distinct accent colors", () => {
    expect(cardAccent(card({ group: "DBT_SOURCE", label: "raw_orders" }))).not.toBe(
      cardAccent(card({ label: "fct_payments" })),
    );
  });
});

describe("lineage-type lens", () => {
  const lens = getLens("lineage-type");
  it("badges columns P/R/T/U", () => {
    expect(lens.columnBadge(col({ lineageType: "pass-through" }))!.text).toBe("P");
    expect(lens.columnBadge(col({ lineageType: "rename" }))!.text).toBe("R");
    expect(lens.columnBadge(col({ lineageType: "transformation" }))!.text).toBe("T");
    expect(lens.columnBadge(col({ lineageType: "unknown" }))!.text).toBe("U");
  });
  it("exposes a 4-item legend labelled Transformations", () => {
    expect(lens.label).toBe("Transformations");
    expect(lens.legend).toHaveLength(4);
  });
});

describe("getLens", () => {
  it("defaults to lineage-type for unknown ids", () => {
    expect(getLens("nope").id).toBe("lineage-type");
    expect(LENS_ORDER[0]).toBe("lineage-type");
  });
});
