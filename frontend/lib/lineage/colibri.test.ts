import { describe, it, expect } from "vitest";
import {
  detectModelType,
  aggregateLineageType,
  showModelTypeBadge,
  MODEL_TYPE_META,
  LINEAGE_TYPE_META,
  LINEAGE_TYPE_ORDER,
} from "@/lib/lineage/colibri";
import { buildColumnModel } from "@/lib/lineage/column-model";
import { NODES, LINKS, CENTER } from "@/lib/lineage/__fixtures__";

describe("detectModelType (port of colibri detect_model_type)", () => {
  it("classifies by dbt naming prefix", () => {
    expect(detectModelType("dim_customer")).toBe("dimension");
    expect(detectModelType("fact_orders")).toBe("fact");
    expect(detectModelType("int_payments_joined")).toBe("intermediate");
    expect(detectModelType("stg_stripe__payments")).toBe("staging");
    expect(detectModelType("reporting_revenue")).toBe("unknown");
  });

  it("matches colibri's double-underscore staging names", () => {
    // the real memory-verified model name uses `stg__`
    expect(detectModelType("stg__driver_recruitment_goals")).toBe("staging");
  });

  it("uses the last dotted segment, like colibri's node_id.split('.')[-1]", () => {
    expect(detectModelType("model.project.dim_date")).toBe("dimension");
    expect(detectModelType("model.project.fact_trips.v2")).toBe("unknown"); // last segment is the version
  });

  it("is null/empty safe", () => {
    expect(detectModelType(null)).toBe("unknown");
    expect(detectModelType(undefined)).toBe("unknown");
    expect(detectModelType("")).toBe("unknown");
  });
});

describe("aggregateLineageType (port of colibri build_full_lineage aggregation)", () => {
  it("≥2 upstream parents is a transformation", () => {
    expect(aggregateLineageType(2)).toBe("transformation");
    expect(aggregateLineageType(5)).toBe("transformation");
  });
  it("exactly one upstream parent is a pass-through", () => {
    expect(aggregateLineageType(1)).toBe("pass-through");
  });
  it("no upstream parent is unknown (origin column)", () => {
    expect(aggregateLineageType(0)).toBe("unknown");
  });
});

describe("showModelTypeBadge", () => {
  it("shows only for dbt groups with a known model type", () => {
    expect(showModelTypeBadge("DBT_MODEL", "staging")).toBe(true);
    expect(showModelTypeBadge("DBT_SOURCE", "dimension")).toBe(true);
    expect(showModelTypeBadge("DBT_MODEL", "unknown")).toBe(false);
    expect(showModelTypeBadge("PB_TABLE", "fact")).toBe(false);
    expect(showModelTypeBadge(null, "fact")).toBe(false);
  });
});

describe("metadata tables are complete", () => {
  it("every model type has badge/color/label", () => {
    for (const t of ["dimension", "fact", "intermediate", "staging", "unknown"] as const) {
      expect(MODEL_TYPE_META[t]).toBeTruthy();
      expect(MODEL_TYPE_META[t].label).toBeTruthy();
    }
  });
  it("every legend lineage type resolves", () => {
    for (const t of LINEAGE_TYPE_ORDER) {
      expect(LINEAGE_TYPE_META[t]).toBeTruthy();
      expect(LINEAGE_TYPE_META[t].label).toBeTruthy();
    }
  });
});

describe("column model carries colibri classification", () => {
  const model = buildColumnModel(NODES, LINKS, CENTER);

  it("tags the staging card with its model type", () => {
    const stg = model.cards.find((c) => c.id === "DBT_MODEL::stg1")!;
    expect(stg.modelType).toBe("staging");
  });

  it("derives lineage type structurally from upstream parent count", () => {
    // date_actual is an origin column (no upstream) -> unknown
    const stg = model.cards.find((c) => c.id === "DBT_MODEL::stg1")!;
    expect(stg.columns[0].lineageType).toBe("unknown");

    // the PB column `date` has exactly one upstream (the dbt bridge) -> pass-through
    const calendar = model.cards.find((c) => c.id === "PB_TABLE::t1")!;
    const dateCol = calendar.columns.find((c) => c.id === "PB_COLUMN::c1")!;
    expect(dateCol.lineageType).toBe("pass-through");

    // the measure has exactly one upstream (the pb column) -> pass-through
    const measure = calendar.columns.find((c) => c.id === "PB_MEASURE::m1")!;
    expect(measure.lineageType).toBe("pass-through");
  });
});
