import { describe, it, expect } from "vitest";
import { buildColumnModel, columnLineage, dbtBuildCommand } from "@/lib/lineage/column-model";
import { NODES, LINKS, CENTER } from "@/lib/lineage/__fixtures__";
import type { NetworkNode, NetworkLink } from "@/lib/api";

describe("buildColumnModel", () => {
  const model = buildColumnModel(NODES, LINKS, CENTER);

  it("groups columns into model cards", () => {
    expect(model.cards).toHaveLength(2);
    const calendar = model.cards.find((c) => c.id === "PB_TABLE::t1")!;
    expect(calendar.columns).toHaveLength(2);
    const stg = model.cards.find((c) => c.id === "DBT_MODEL::stg1")!;
    expect(stg.columns).toHaveLength(1);
    expect(stg.columns[0].label).toBe("date_actual");
  });

  it("tags the measure row with the Σ glyph", () => {
    const calendar = model.cards.find((c) => c.id === "PB_TABLE::t1")!;
    const measure = calendar.columns.find((col) => col.id === "PB_MEASURE::m1")!;
    expect(measure.isMeasure).toBe(true);
    expect(measure.glyph).toBe("Σ");
  });

  it("maps columns to their owning card", () => {
    expect(model.colToCard["PB_MEASURE::m1"]).toBe("PB_TABLE::t1");
    expect(model.colToCard["DBT_COLUMN::d1"]).toBe("DBT_MODEL::stg1");
  });

  it("marks the cross-tool edge as a bridge", () => {
    const bridge = model.edges.find((e) => e.sourceHandle === "DBT_COLUMN::d1");
    expect(bridge).toBeTruthy();
    expect(bridge!.bridge).toBe(true);
    expect(bridge!.source).toBe("DBT_MODEL::stg1");
    expect(bridge!.target).toBe("PB_TABLE::t1");
  });

  it("identifies the centered measure as a column", () => {
    expect(model.centerColId).toBe(CENTER);
  });

  it("synthesizes a container for orphan members", () => {
    const orphanNodes = [{ id: "PB_MEASURE::x", group: "PB_MEASURE", label: "Orphan" }];
    const m = buildColumnModel(orphanNodes, [], null);
    expect(m.cards).toHaveLength(1);
    expect(m.cards[0].id).toBe("__grp__PB_MEASURE");
    expect(m.cards[0].label).toBe("Measures");
  });

  it("tags every card with a cardKind", () => {
    expect(model.cards.every((c) => c.cardKind === "model")).toBe(true);
  });

  it("always renders the focused container, even with no loaded columns", () => {
    // Selecting a single element (depth-0 focus): only the container node comes
    // back, with no members/contains edges — it must still appear as a card.
    const lone: NetworkNode[] = [{ id: "DBT_MODEL::solo", group: "DBT_MODEL", label: "solo_model" }];
    const m = buildColumnModel(lone, [], "DBT_MODEL::solo");
    expect(m.cards).toHaveLength(1);
    expect(m.cards[0].id).toBe("DBT_MODEL::solo");
    expect(m.cards[0].isCenter).toBe(true);
    expect(m.cards[0].columns).toHaveLength(0);
  });
});

describe("buildColumnModel — measuresAsOwnCards", () => {
  const nodes: NetworkNode[] = [
    { id: "PB_TABLE::t1", group: "PB_TABLE", label: "Calendar" },
    { id: "PB_MEASURE::m1", group: "PB_MEASURE", label: "Revenue", dataset: "Sales" },
    { id: "PB_MEASURE::m2", group: "PB_MEASURE", label: "Cost", dataset: "Sales" },
    { id: "PB_MEASURE::m3", group: "PB_MEASURE", label: "Visits", dataset: "Web" },
  ];
  const links: NetworkLink[] = [
    { source: "PB_TABLE::t1", target: "PB_MEASURE::m1", kind: "contains" },
    { source: "PB_TABLE::t1", target: "PB_MEASURE::m2", kind: "contains" },
    { source: "PB_TABLE::t1", target: "PB_MEASURE::m3", kind: "contains" },
  ];

  it("promotes every measure to its own one-row card", () => {
    const m = buildColumnModel(nodes, links, null, { measuresAsOwnCards: true });
    const measureCards = m.cards.filter((c) => c.cardKind === "measures");
    // one card per measure, each labelled by the measure and carrying just it
    expect(measureCards.map((c) => c.label).sort()).toEqual(["Cost", "Revenue", "Visits"]);
    for (const c of measureCards) expect(c.columns).toHaveLength(1);
    const revenue = measureCards.find((c) => c.label === "Revenue")!;
    expect(revenue.columns[0].id).toBe("PB_MEASURE::m1");
    expect(revenue.columns[0].isMeasure).toBe(true);
    expect(revenue.datasetId).toBe("Sales");
    // each measure maps to its own card, not the shared home table
    expect(m.colToCard["PB_MEASURE::m1"]).toBe("__measure__::PB_MEASURE::m1");
    // the home table no longer carries the measures (and so drops out, empty)
    expect(m.cards.find((c) => c.id === "PB_TABLE::t1")).toBeUndefined();
  });

  it("leaves measures in their home table when the option is off (default)", () => {
    const m = buildColumnModel(nodes, links, null);
    expect(m.cards.some((c) => c.cardKind === "measures")).toBe(false);
    expect(m.colToCard["PB_MEASURE::m1"]).toBe("PB_TABLE::t1");
  });
});

describe("buildColumnModel — includeReportCards", () => {
  const nodes: NetworkNode[] = [
    { id: "PB_TABLE::t", group: "PB_TABLE", label: "Sales" },
    { id: "PB_MEASURE::m", group: "PB_MEASURE", label: "Revenue", dataset: "Sales" },
    { id: "PB_VISUAL::v", group: "PB_VISUAL", label: "Bar chart" },
    { id: "PB_PAGE::pg", group: "PB_PAGE", label: "Overview" },
    { id: "PB_REPORT::r", group: "PB_REPORT", label: "Exec" },
  ];
  const links: NetworkLink[] = [
    { source: "PB_TABLE::t", target: "PB_MEASURE::m", kind: "contains" },
    { source: "PB_MEASURE::m", target: "PB_VISUAL::v", kind: "model" },
    { source: "PB_VISUAL::v", target: "PB_PAGE::pg", kind: "model" },
    { source: "PB_PAGE::pg", target: "PB_REPORT::r", kind: "model" },
  ];

  it("adds report/page/visual cards wired by usage edges", () => {
    const m = buildColumnModel(nodes, links, null, { includeReportCards: true });
    const reportCards = m.cards.filter((c) => c.cardKind === "report");
    expect(reportCards.map((c) => c.id).sort()).toEqual(["PB_PAGE::pg", "PB_REPORT::r", "PB_VISUAL::v"]);
    const usage = m.usageEdges ?? [];
    // member → visual carries the producing column's handle
    const mv = usage.find((u) => u.target === "PB_VISUAL::v")!;
    expect(mv.source).toBe("PB_TABLE::t");
    expect(mv.sourceHandle).toBe("PB_MEASURE::m");
    // visual → page → report are card-to-card (no source handle)
    expect(usage.some((u) => u.source === "PB_VISUAL::v" && u.target === "PB_PAGE::pg" && !u.sourceHandle)).toBe(true);
    expect(usage.some((u) => u.source === "PB_PAGE::pg" && u.target === "PB_REPORT::r")).toBe(true);
  });

  it("omits report cards by default", () => {
    const m = buildColumnModel(nodes, links, null);
    expect(m.cards.some((c) => c.cardKind === "report")).toBe(false);
    expect(m.usageEdges ?? []).toHaveLength(0);
  });
});

describe("dbtBuildCommand", () => {
  it("targets a dbt model and everything downstream", () => {
    expect(dbtBuildCommand({ label: "stg_orders", group: "DBT_MODEL", cardKind: "model" })).toBe(
      "dbt build -s stg_orders+",
    );
  });
  it("returns null for non-dbt / synthetic cards", () => {
    expect(dbtBuildCommand({ label: "Sales", group: "PB_TABLE", cardKind: "model" })).toBeNull();
    expect(dbtBuildCommand({ label: "Measures", group: "PB_MEASURE", cardKind: "measures" })).toBeNull();
  });
});

describe("columnLineage", () => {
  const model = buildColumnModel(NODES, LINKS, CENTER);

  it("traces a measure all the way upstream to the staging column", () => {
    const { cols, edges, cards } = columnLineage(model, CENTER);
    expect(cols.has("PB_MEASURE::m1")).toBe(true);
    expect(cols.has("PB_COLUMN::c1")).toBe(true);
    expect(cols.has("DBT_COLUMN::d1")).toBe(true);
    // both lineage edges are on the active path
    expect(edges.size).toBe(2);
    // both owning cards are active
    expect(cards.has("PB_TABLE::t1")).toBe(true);
    expect(cards.has("DBT_MODEL::stg1")).toBe(true);
  });

  it("includes downstream when starting from the staging column", () => {
    const { cols } = columnLineage(model, "DBT_COLUMN::d1");
    expect(cols.has("PB_COLUMN::c1")).toBe(true);
    expect(cols.has("PB_MEASURE::m1")).toBe(true);
  });
});

describe("structural (join) edges", () => {
  const nodes = [
    { id: "PB_TABLE::a", group: "PB_TABLE", label: "Orders" },
    { id: "PB_TABLE::b", group: "PB_TABLE", label: "Customers" },
    { id: "PB_COLUMN::ca", group: "PB_COLUMN", label: "customer_id" },
    { id: "PB_COLUMN::cb", group: "PB_COLUMN", label: "id" },
  ];
  const links = [
    { source: "PB_TABLE::a", target: "PB_COLUMN::ca", kind: "contains" },
    { source: "PB_TABLE::b", target: "PB_COLUMN::cb", kind: "contains" },
    { source: "PB_COLUMN::ca", target: "PB_COLUMN::cb", kind: "join" },
  ];

  it("renders join edges but excludes them from lineage adjacency", () => {
    const model = buildColumnModel(nodes, links, null);
    const join = model.edges.find((e) => e.kind === "join");
    expect(join).toBeTruthy();
    expect(join!.sourceHandle).toBe("PB_COLUMN::ca");
    // structural edges must not contribute to data-lineage adjacency
    expect(model.adjForward["PB_COLUMN::ca"]).toBeUndefined();
    expect(model.adjReverse["PB_COLUMN::cb"]).toBeUndefined();
    // …so clicking the FK column does not pull the PK column into the trace
    const { cols } = columnLineage(model, "PB_COLUMN::ca");
    expect(cols.has("PB_COLUMN::cb")).toBe(false);
  });
});
