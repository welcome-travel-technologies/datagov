export const PB_TYPES = [
  { value: "PB_REPORT", label: "PB Report" },
  { value: "PB_PAGE", label: "PB Page" },
  { value: "PB_VISUAL", label: "PB Visual" },
  { value: "PB_TABLE", label: "PB Table" },
  { value: "PB_MEASURE", label: "PB Measure" },
  { value: "PB_COLUMN", label: "PB Column" },
  { value: "PB_FIELD", label: "PB Field" },
];

export const DBT_TYPES = [
  { value: "DBT_MODEL", label: "dbt Model" },
  { value: "DBT_SOURCE", label: "dbt Source" },
  { value: "DBT_SEED", label: "dbt Seed" },
  { value: "DBT_TEST", label: "dbt Test" },
  { value: "DBT_COLUMN", label: "dbt Column" },
];

export const ALL_TYPES = [...PB_TYPES, ...DBT_TYPES];
