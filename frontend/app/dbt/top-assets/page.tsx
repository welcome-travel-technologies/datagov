"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { GitBranch } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { Stat, LoadingState, EmptyState } from "@/components/ui/misc";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { GROUP_LABELS, colorFor } from "@/lib/lineage/graph-utils";
import { api } from "@/lib/api";
import { fmtInt } from "@/lib/utils";

function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}

interface TopByReport {
  item_id: string;
  item_name: string;
  item_type: string;
  connected_reports: number;
  database_name?: string | null;
  schema_name?: string | null;
}

interface TopByFanout {
  item_id: string;
  item_name: string;
  consumers: number;
}

function lineageHref(itemType: string, itemId: string) {
  return `/lineage?node_id=${encodeURIComponent(`${itemType}::${itemId}`)}`;
}

function TypeBadge({ itemType }: { itemType: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-[12px]">
      <span className="h-2 w-2 rounded-full" style={{ background: colorFor(itemType) }} />
      {GROUP_LABELS[itemType] ?? itemType}
    </span>
  );
}

export default function DbtTopAssetsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["dbt-insights", "all"],
    queryFn: () => api.dbtInsights({ section: "all" }),
    staleTime: 60_000,
  });

  const totals = (data?.totals as Record<string, unknown>) ?? {};
  const topByReports = (data?.top_by_reports as TopByReport[] | undefined) ?? [];
  const topByFanout = (data?.top_by_fanout as TopByFanout[] | undefined) ?? [];

  return (
    <div>
      <PageHeader
        title="dbt Top Assets & Impact"
        description="The most-connected dbt models, ranked by downstream usage."
      />

      <div className="mb-5 grid grid-cols-2 gap-4 md:grid-cols-3">
        <Stat label="Models" value={fmtInt(num(totals.models))} />
        <Stat label="Sources" value={fmtInt(num(totals.sources))} />
        <Stat label="Columns" value={fmtInt(num(totals.columns))} />
      </div>

      {isLoading && (
        <Card>
          <LoadingState label="Calculating dbt impact…" />
        </Card>
      )}
      {isError && (
        <Card>
          <EmptyState title="Failed to load" hint="The dbt insights API returned an error." />
        </Card>
      )}

      {!isLoading && !isError && (
        <Tabs defaultValue="reports">
          <TabsList>
            <TabsTrigger value="reports">Top by Report Impact</TabsTrigger>
            <TabsTrigger value="fanout">Top by Fan-out</TabsTrigger>
          </TabsList>

          <TabsContent value="reports">
            <Card className="overflow-hidden">
              {topByReports.length === 0 ? (
                <EmptyState title="No impact data yet" hint="Has the dbt → PowerBI bridge run?" />
              ) : (
                <Table>
                  <THead>
                    <TR>
                      <TH>Name</TH>
                      <TH>Type</TH>
                      <TH className="text-right">Reports</TH>
                      <TH>Database / Schema</TH>
                      <TH className="text-right">Lineage</TH>
                    </TR>
                  </THead>
                  <TBody>
                    {topByReports.map((r) => (
                      <TR key={r.item_id}>
                        <TD className="max-w-[320px]">
                          <span className="block truncate font-medium">{r.item_name}</span>
                        </TD>
                        <TD>
                          <TypeBadge itemType={r.item_type} />
                        </TD>
                        <TD className="text-right font-medium">{fmtInt(num(r.connected_reports))}</TD>
                        <TD className="text-[12.5px] text-muted-foreground">
                          {[r.database_name, r.schema_name].filter(Boolean).join(".") || "—"}
                        </TD>
                        <TD className="text-right">
                          <Link
                            href={lineageHref(r.item_type, r.item_id)}
                            className="inline-flex items-center gap-1 text-brand hover:underline"
                          >
                            <GitBranch className="h-3.5 w-3.5" />
                          </Link>
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              )}
            </Card>
          </TabsContent>

          <TabsContent value="fanout">
            <Card className="overflow-hidden">
              {topByFanout.length === 0 ? (
                <EmptyState title="No fan-out data yet" hint="Has the dbt lineage been ingested?" />
              ) : (
                <Table>
                  <THead>
                    <TR>
                      <TH>Name</TH>
                      <TH className="text-right">Consumers</TH>
                      <TH className="text-right">Lineage</TH>
                    </TR>
                  </THead>
                  <TBody>
                    {topByFanout.map((r) => (
                      <TR key={r.item_id}>
                        <TD className="max-w-[320px]">
                          <span className="block truncate font-medium">{r.item_name}</span>
                        </TD>
                        <TD className="text-right font-medium">{fmtInt(num(r.consumers))}</TD>
                        <TD className="text-right">
                          <Link
                            href={lineageHref("DBT_MODEL", r.item_id)}
                            className="inline-flex items-center gap-1 text-brand hover:underline"
                          >
                            <GitBranch className="h-3.5 w-3.5" />
                          </Link>
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              )}
            </Card>
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}
