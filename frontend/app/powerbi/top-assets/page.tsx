"use client";

import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { CatalogView } from "@/components/items/catalog-view";
import { PB_TYPES } from "@/components/items/type-options";
import { Stat } from "@/components/ui/misc";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { fmtInt } from "@/lib/utils";

function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}

export default function PowerBiTopAssetsPage() {
  const { data: summary } = useQuery({ queryKey: ["summary"], queryFn: api.summary, staleTime: 60_000 });

  return (
    <div>
      <PageHeader
        title="PowerBI Top Assets & Impact"
        description="The most-connected PowerBI assets, ranked by downstream usage."
      />

      <div className="mb-5 grid grid-cols-2 gap-4 md:grid-cols-3">
        <Stat label="Measures" value={fmtInt(num(summary?.total_measures))} />
        <Stat label="Columns" value={fmtInt(num(summary?.total_columns))} />
        <Stat label="Reports" value={fmtInt(num(summary?.total_reports))} />
      </div>

      <Tabs defaultValue="reports">
        <TabsList>
          <TabsTrigger value="reports">Top by Report Impact</TabsTrigger>
          <TabsTrigger value="visuals">Top by Visual Usage</TabsTrigger>
        </TabsList>

        <TabsContent value="reports">
          <CatalogView
            baseParams={{ service: "powerbi" }}
            typeOptions={PB_TYPES}
            showWorkspaceFilter
            defaultOrdering="-connected_reports"
            columns={[
              { key: "connected_reports", label: "Reports", sortable: true },
              { key: "connected_visuals", label: "Visuals", sortable: true },
            ]}
          />
        </TabsContent>

        <TabsContent value="visuals">
          <CatalogView
            baseParams={{ service: "powerbi" }}
            typeOptions={PB_TYPES}
            showWorkspaceFilter
            defaultOrdering="-connected_visuals"
            columns={[
              { key: "connected_visuals", label: "Visuals", sortable: true },
              { key: "connected_reports", label: "Reports", sortable: true },
            ]}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
