import type { NetworkNode, NetworkLink } from "@/lib/api";

/**
 * The real, memory-verified cross-tool scenario: the measure
 * "%YoY Tours Upsells {B}" traces upstream through a PowerBI column and a
 * dbt↔PowerBI bridge to the staging column `date_actual` in
 * `stg__driver_recruitment_goals`.
 */
export const NODES: NetworkNode[] = [
  { id: "PB_MEASURE::m1", group: "PB_MEASURE", label: "%YoY Tours Upsells {B}" },
  { id: "PB_COLUMN::c1", group: "PB_COLUMN", label: "date", datatype: "date" },
  { id: "PB_TABLE::t1", group: "PB_TABLE", label: "Calendar" },
  { id: "DBT_COLUMN::d1", group: "DBT_COLUMN", label: "date_actual", datatype: "timestamp" },
  { id: "DBT_MODEL::stg1", group: "DBT_MODEL", label: "stg__driver_recruitment_goals" },
];

export const LINKS: NetworkLink[] = [
  { source: "PB_TABLE::t1", target: "PB_MEASURE::m1", kind: "contains" },
  { source: "PB_TABLE::t1", target: "PB_COLUMN::c1", kind: "contains" },
  { source: "DBT_MODEL::stg1", target: "DBT_COLUMN::d1", kind: "contains" },
  // lineage: pb column feeds the measure (same card)
  { source: "PB_COLUMN::c1", target: "PB_MEASURE::m1", kind: "column" },
  // cross-tool bridge: dbt staging column feeds the pb column
  { source: "DBT_COLUMN::d1", target: "PB_COLUMN::c1", kind: "column" },
];

export const CENTER = "PB_MEASURE::m1";
