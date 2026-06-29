import { describe, it, expect } from "vitest";
import {
  cleanLabel,
  nodeDisplayLabel,
  typeGlyph,
  memberGlyph,
  isBridge,
  isColumnGroup,
  isMemberGroup,
  isDbtGroup,
  edgeClasses,
  colorFor,
} from "@/lib/lineage/graph-utils";

describe("cleanLabel", () => {
  it("strips a trailing disambiguation hash", () => {
    expect(cleanLabel("Revenue (c9f60527abce)")).toBe("Revenue");
  });
  it("strips a leading TYPE:: id prefix", () => {
    expect(cleanLabel("PB_MEASURE::abc123")).toBe("abc123");
  });
  it("leaves clean labels untouched", () => {
    expect(cleanLabel("Total Sales")).toBe("Total Sales");
  });
  it("handles null/undefined", () => {
    expect(cleanLabel(null)).toBe("");
    expect(cleanLabel(undefined)).toBe("");
  });
});

describe("nodeDisplayLabel", () => {
  it("prefixes columns with their parent", () => {
    expect(
      nodeDisplayLabel({ id: "x", label: "is_valid", group: "DBT_COLUMN", parent: "stg_users" }),
    ).toBe("stg_users.is_valid");
  });
  it("does not prefix non-columns", () => {
    expect(nodeDisplayLabel({ id: "x", label: "Total Sales", group: "PB_MEASURE", parent: "Sales" })).toBe(
      "Total Sales",
    );
  });
});

describe("typeGlyph", () => {
  it("maps datatypes to colibri glyphs", () => {
    expect(typeGlyph("varchar")).toBe("Aa");
    expect(typeGlyph("integer")).toBe("#");
    expect(typeGlyph("timestamp")).toBe("◷");
    expect(typeGlyph("boolean")).toBe("⊨");
    expect(typeGlyph("")).toBe("•");
  });
});

describe("memberGlyph", () => {
  it("uses Σ for measures", () => {
    expect(memberGlyph({ group: "PB_MEASURE", datatype: null })).toBe("Σ");
  });
  it("falls back to a datatype glyph for columns", () => {
    expect(memberGlyph({ group: "DBT_COLUMN", datatype: "int" })).toBe("#");
  });
});

describe("group predicates", () => {
  it("classifies dbt / column / member groups", () => {
    expect(isDbtGroup("DBT_MODEL")).toBe(true);
    expect(isDbtGroup("PB_TABLE")).toBe(false);
    expect(isColumnGroup("PB_COLUMN")).toBe(true);
    expect(isColumnGroup("PB_MEASURE")).toBe(false);
    expect(isMemberGroup("PB_MEASURE")).toBe(true);
    expect(isMemberGroup("DBT_COLUMN")).toBe(true);
    expect(isMemberGroup("PB_TABLE")).toBe(false);
  });
});

describe("isBridge / edgeClasses", () => {
  it("detects cross-tool bridges", () => {
    expect(isBridge("DBT_COLUMN::a", "PB_COLUMN::b")).toBe(true);
    expect(isBridge("PB_COLUMN::a", "PB_MEASURE::b")).toBe(false);
    expect(isBridge("DBT_COLUMN::a", "DBT_COLUMN::b")).toBe(false);
  });
  it("classifies a cross-tool column edge as a bridge", () => {
    const cls = edgeClasses({ source: "DBT_COLUMN::a", target: "PB_COLUMN::b", kind: "column" });
    expect(cls).toEqual({ column: true, bridge: true });
  });
  it("classifies a same-tool column edge as a plain column edge", () => {
    const cls = edgeClasses({ source: "PB_COLUMN::a", target: "PB_MEASURE::b", kind: "column" });
    expect(cls).toEqual({ column: true, bridge: false });
  });
  it("classifies a model edge as neither", () => {
    const cls = edgeClasses({ source: "DBT_MODEL::a", target: "DBT_MODEL::b", kind: "model" });
    expect(cls).toEqual({ column: false, bridge: false });
  });
});

describe("colorFor", () => {
  it("returns a known color and a fallback", () => {
    expect(colorFor("PB_MEASURE")).toBe("#10b981");
    expect(colorFor("SOMETHING_ELSE")).toBe(colorFor("UNKNOWN"));
  });
});
