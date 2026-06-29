"use client";

import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import {
  CleanupView,
  asCleanupRows,
  type CleanupStat,
  type CleanupTab,
} from "@/components/cleanup/cleanup-view";
import { api } from "@/lib/api";

/** Shape of /api/dbt-insights/?section=cleanup (only the bits we use). */
interface DbtCleanupInsights {
  totals?: Record<string, number>;
  unused_models?: unknown;
  unused_seeds?: unknown;
  unused_sources?: unknown;
  undocumented_models?: unknown;
  untested_models?: unknown;
}

const INSIGHTS_KEY = ["dbt-insights", "cleanup"] as const;

export default function DbtCleanupPage() {
  const insightsQ = useQuery({
    queryKey: INSIGHTS_KEY,
    queryFn: () => api.dbtInsights({ section: "cleanup" }) as Promise<DbtCleanupInsights>,
  });

  if (insightsQ.isLoading) {
    return (
      <div>
        <PageHeader title="dbt Cleanup Opportunities" />
        <LoadingState label="Scanning dbt layer for cleanup opportunities…" />
      </div>
    );
  }
  if (insightsQ.isError || !insightsQ.data) {
    return (
      <div>
        <PageHeader title="dbt Cleanup Opportunities" />
        <EmptyState title="Failed to load" hint="The dbt insights API returned an error." />
      </div>
    );
  }

  const d = insightsQ.data;
  const t = d.totals ?? {};

  const stats: CleanupStat[] = [
    { key: "unused_models", label: "Unused Models", value: t.unused_models ?? 0, accent: "danger" },
    { key: "unused_seeds", label: "Unused Seeds", value: t.unused_seeds ?? 0, accent: "danger" },
    { key: "unused_sources", label: "Unused Sources", value: t.unused_sources ?? 0, accent: "danger" },
    { key: "undocumented_models", label: "Undocumented Models", value: t.undocumented_models ?? 0, accent: "warning" },
    { key: "untested_models", label: "Untested Models", value: t.untested_models ?? 0, accent: "warning" },
    { key: "attention", label: "Needs Attention", value: t.attention ?? 0, accent: "warning" },
    { key: "deprecated", label: "Deleted", value: t.deprecated ?? 0, accent: "muted" },
  ];

  const tabs: CleanupTab[] = [
    {
      kind: "rows",
      key: "unused_models",
      label: "Unused Models",
      count: t.unused_models,
      rows: asCleanupRows(d.unused_models),
    },
    {
      kind: "rows",
      key: "unused_sources",
      label: "Unused Sources",
      count: t.unused_sources,
      rows: asCleanupRows(d.unused_sources),
    },
    {
      kind: "rows",
      key: "unused_seeds",
      label: "Unused Seeds",
      count: t.unused_seeds,
      rows: asCleanupRows(d.unused_seeds),
    },
    {
      kind: "rows",
      key: "undocumented_models",
      label: "Undocumented Models",
      count: t.undocumented_models,
      rows: asCleanupRows(d.undocumented_models),
    },
    {
      kind: "rows",
      key: "untested_models",
      label: "Untested Models",
      count: t.untested_models,
      rows: asCleanupRows(d.untested_models),
    },
    {
      kind: "query",
      key: "deprecated",
      label: "Deleted",
      count: t.deprecated,
      deletedTab: true,
      params: { status: "DELETED", include_deleted: "true" },
    },
  ];

  return (
    <div>
      <PageHeader
        title="dbt Cleanup Opportunities"
        description="dbt models and sources nobody reads, models without tests or docs. Select rows and mark a group to delete — it moves to the Deleted tab where you can undo (restoring it and resetting status to Unverified)."
      />
      <CleanupView
        service="dbt"
        stats={stats}
        tabs={tabs}
        countsKey={[...INSIGHTS_KEY]}
      />
    </div>
  );
}
