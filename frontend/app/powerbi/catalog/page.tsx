"use client";

import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { CatalogView } from "@/components/items/catalog-view";
import { PB_TYPES } from "@/components/items/type-options";
import { Stat } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { fmtInt } from "@/lib/utils";

function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}

export default function PowerBiCatalogPage() {
  const { data: summary } = useQuery({ queryKey: ["summary"], queryFn: api.summary, staleTime: 60_000 });

  const kpis = (
    <div className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-5">
      <Stat label="Total Measures" value={fmtInt(num(summary?.total_measures))} />
      <Stat label="Unused Measures" value={fmtInt(num(summary?.unused_measures))} accent />
      <Stat label="Total Columns" value={fmtInt(num(summary?.total_columns))} />
      <Stat label="Unused Columns" value={fmtInt(num(summary?.unused_columns))} accent />
      <Stat label="Total Reports" value={fmtInt(num(summary?.total_reports))} />
    </div>
  );

  return (
    <div>
      <PageHeader title="PowerBI Catalog" description="Reports, datasets, tables, measures, columns and fields." />
      <CatalogView
        baseParams={{ service: "powerbi" }}
        typeOptions={PB_TYPES}
        kpis={kpis}
        showWorkspaceFilter
        showDatasetFilter
        showStatusFilter
        showUsageFilter
        columns={[
          { key: "connected_reports", label: "Reports", sortable: true },
          { key: "connected_visuals", label: "Visuals", sortable: true },
          { key: "is_unused", label: "Status" },
        ]}
      />
    </div>
  );
}
