"use client";

import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { CatalogView } from "@/components/items/catalog-view";
import { DBT_TYPES } from "@/components/items/type-options";
import { Stat } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { fmtInt } from "@/lib/utils";

function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}

export default function DbtCatalogPage() {
  const { data: insights } = useQuery({
    queryKey: ["dbt-insights", "all"],
    queryFn: () => api.dbtInsights({ section: "all" }),
    staleTime: 60_000,
  });
  const totals = (insights?.totals as Record<string, unknown>) ?? {};

  const kpis = (
    <div className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-4 xl:grid-cols-7">
      <Stat label="Models" value={fmtInt(num(totals.models))} />
      <Stat label="Sources" value={fmtInt(num(totals.sources))} />
      <Stat label="Seeds" value={fmtInt(num(totals.seeds))} />
      <Stat label="Tests" value={fmtInt(num(totals.tests))} />
      <Stat label="Columns" value={fmtInt(num(totals.columns))} />
      <Stat label="Undocumented Models" value={fmtInt(num(totals.undocumented_models))} accent />
      <Stat label="Untested Models" value={fmtInt(num(totals.untested_models))} accent />
    </div>
  );

  return (
    <div>
      <PageHeader title="dbt Catalog" description="Models, sources, seeds, tests and columns from your dbt project." />
      <CatalogView
        baseParams={{ service: "dbt" }}
        typeOptions={DBT_TYPES}
        kpis={kpis}
        showWorkspaceFilter
        showDatasetFilter
        columns={[
          { key: "connected_columns", label: "Columns", sortable: true },
          { key: "connected_tables", label: "Tables", sortable: true },
        ]}
      />
    </div>
  );
}
