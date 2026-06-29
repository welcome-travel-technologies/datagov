import { describe, it, expect } from "vitest";
import { buildLineageTree, buildLineageTreeByPath, countLeaves, type TreeBranch } from "@/lib/lineage/tree";
import type { NetworkNode } from "@/lib/api";

function leafIds(b: TreeBranch): string[] {
  return [...b.leaves.map((l) => l.id), ...b.children.flatMap(leafIds)];
}

describe("buildLineageTree", () => {
  const nodes: NetworkNode[] = [
    { id: "DBT_MODEL::a", label: "stg_orders", group: "DBT_MODEL", database: "analytics", schema: "staging" },
    { id: "DBT_MODEL::b", label: "fct_orders", group: "DBT_MODEL", database: "analytics", schema: "marts" },
    { id: "PB_TABLE::t", label: "Sales", group: "PB_TABLE", workspace_name: "Finance", dataset: "SalesModel" },
    { id: "DBT_COLUMN::c", label: "amount", group: "DBT_COLUMN" },
  ];

  it("splits into dbt and Power BI roots", () => {
    const roots = buildLineageTree(nodes);
    expect(roots.map((r) => r.label).sort()).toEqual(["Power BI", "dbt"]);
  });

  it("groups dbt assets by database → schema", () => {
    const dbt = buildLineageTree(nodes).find((r) => r.label === "dbt")!;
    expect(countLeaves(dbt)).toBe(2);
    const analytics = dbt.children.find((c) => c.label === "analytics")!;
    expect(analytics.children.map((c) => c.label)).toEqual(["marts", "staging"]); // sorted
  });

  it("groups Power BI tables by workspace → dataset", () => {
    const pbi = buildLineageTree(nodes).find((r) => r.label === "Power BI")!;
    const finance = pbi.children.find((c) => c.label === "Finance")!;
    expect(finance.children[0].label).toBe("SalesModel");
    expect(finance.children[0].leaves[0].id).toBe("PB_TABLE::t");
  });

  it("excludes non-asset nodes (columns/measures) from leaves", () => {
    const roots = buildLineageTree(nodes);
    const ids = roots.flatMap(leafIds);
    expect(ids).not.toContain("DBT_COLUMN::c");
    expect(ids.sort()).toEqual(["DBT_MODEL::a", "DBT_MODEL::b", "PB_TABLE::t"]);
  });

  it("falls back gracefully when grouping keys are missing", () => {
    const roots = buildLineageTree([{ id: "DBT_SOURCE::s", label: "raw", group: "DBT_SOURCE" } as NetworkNode]);
    expect(roots).toHaveLength(1);
    expect(countLeaves(roots[0])).toBe(1);
  });
});

describe("buildLineageTreeByPath", () => {
  it("nests dbt assets under their real file path (filename dropped)", () => {
    const nodes: NetworkNode[] = [
      { id: "DBT_MODEL::a", label: "stg_orders", group: "DBT_MODEL", path: "models/staging/stg_orders.sql" },
    ];
    const dbt = buildLineageTreeByPath(nodes).find((r) => r.label === "dbt")!;
    const models = dbt.children.find((c) => c.label === "models")!;
    const staging = models.children.find((c) => c.label === "staging")!;
    expect(staging.leaves[0].id).toBe("DBT_MODEL::a");
  });

  it("derives a folder from the model-type prefix when no path is present", () => {
    const nodes: NetworkNode[] = [
      { id: "DBT_MODEL::a", label: "stg_orders", group: "DBT_MODEL" },
      { id: "DBT_MODEL::b", label: "fct_orders", group: "DBT_MODEL" },
      { id: "DBT_SEED::s", label: "country_codes", group: "DBT_SEED" },
    ];
    const dbt = buildLineageTreeByPath(nodes).find((r) => r.label === "dbt")!;
    expect(dbt.children.map((c) => c.label).sort()).toEqual(["marts", "seeds", "staging"]);
  });

  it("still groups Power BI tables by workspace → dataset", () => {
    const nodes: NetworkNode[] = [
      { id: "PB_TABLE::t", label: "Sales", group: "PB_TABLE", workspace_name: "Finance", dataset: "SalesModel" },
    ];
    const pbi = buildLineageTreeByPath(nodes).find((r) => r.label === "Power BI")!;
    expect(pbi.children[0].label).toBe("Finance");
    expect(pbi.children[0].children[0].label).toBe("SalesModel");
  });
});
