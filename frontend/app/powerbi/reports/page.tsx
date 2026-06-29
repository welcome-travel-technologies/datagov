"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import {
  ReportUsagePivot,
  type DimKey,
  type MeasureKey,
} from "@/components/reports/report-usage-pivot";
import { ReportHealthTable } from "@/components/reports/report-health-table";
import { SimpleSelect } from "@/components/ui/simple-select";

const DEFAULT_MONTH = "__default__";

const SELECT_TRIGGER_CLS = "min-w-[150px]";

export default function PowerBiReportsPage() {
  const [tab, setTab] = useState("usage");
  const [ws, setWs] = useState("");
  const [monthState, setMonthState] = useState(DEFAULT_MONTH);

  // Pivot shape (lifted so it survives tab switches).
  const [pivotY, setPivotY] = useState<DimKey>("report_name");
  const [pivotX, setPivotX] = useState<DimKey>("month");
  const [measure, setMeasure] = useState<MeasureKey>("view_count");

  const filtersQ = useQuery({
    queryKey: ["filters"],
    queryFn: () => api.filters(),
    staleTime: 5 * 60_000,
  });
  // Months come from the usage endpoint (sorted desc); default to most-recent.
  const monthsQ = useQuery({
    queryKey: ["usage-months"],
    queryFn: () => api.powerbiUsage({ limit: 1 }),
    staleTime: 5 * 60_000,
  });

  const months = monthsQ.data?.months ?? [];
  const month = monthState === DEFAULT_MONTH ? months[0] ?? "" : monthState;

  return (
    <div>
      <PageHeader
        title="Report Health & Usage"
        description="Pivot report-view data and inspect report complexity in one place. The leaderboard view lives on the Data Champions page."
      />

      <Card className="overflow-hidden">
        <Tabs value={tab} onValueChange={setTab}>
          {/* Tab nav + shared filters */}
          <div className="border-b border-line bg-panel2/40 px-4 pt-3">
            <TabsList className="border-b-0">
              <TabsTrigger value="usage">Report Usage</TabsTrigger>
              <TabsTrigger value="health">Report Health</TabsTrigger>
            </TabsList>
          </div>

          <div className="flex flex-wrap items-end gap-4 border-b border-line p-4">
            <div>
              <label className="mb-1 block text-[11px] font-medium text-faint">Workspace</label>
              <SimpleSelect
                value={ws}
                onValueChange={setWs}
                className={SELECT_TRIGGER_CLS}
                options={[
                  { value: "", label: "All Workspaces" },
                  ...(filtersQ.data?.workspaces ?? []).map((w) => ({ value: w, label: w })),
                ]}
              />
            </div>
            {tab === "usage" && (
              <div>
                <label className="mb-1 block text-[11px] font-medium text-faint">Month</label>
                <SimpleSelect
                  value={month}
                  onValueChange={setMonthState}
                  className={SELECT_TRIGGER_CLS}
                  options={[
                    { value: "", label: "All Months" },
                    ...months.map((m) => ({ value: m, label: m.substring(0, 7) })),
                  ]}
                />
              </div>
            )}
          </div>

          <TabsContent value="usage" className="mt-0">
            <ReportUsagePivot
              workspace={ws}
              month={month}
              monthsReady={monthsQ.isSuccess}
              pivotY={pivotY}
              pivotX={pivotX}
              measure={measure}
              onChangeY={setPivotY}
              onChangeX={setPivotX}
              onChangeMeasure={setMeasure}
            />
          </TabsContent>

          <TabsContent value="health" className="mt-0">
            <ReportHealthTable workspace={ws} />
          </TabsContent>
        </Tabs>
      </Card>
    </div>
  );
}
