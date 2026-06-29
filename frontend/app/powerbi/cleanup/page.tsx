"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import {
  CleanupView,
  type CleanupStat,
  type CleanupTab,
} from "@/components/cleanup/cleanup-view";
import { api } from "@/lib/api";

export default function PowerBiCleanupPage() {
  const [filters, setFilters] = useState({ workspace_name: "", dataset_name: "" });

  // KPI cards + tab badges. Filter-aware: re-fetched as the workspace/dataset
  // filter changes so the numbers track the filtered table.
  const countsParams = {
    workspace_name: filters.workspace_name || undefined,
    dataset_name: filters.dataset_name || undefined,
  };
  const countsKey = ["pb-cleanup-counts", countsParams] as const;
  const countsQ = useQuery({
    queryKey: countsKey,
    queryFn: () => api.pbCleanupCounts(countsParams),
  });
  const c = countsQ.data ?? {};

  const stats: CleanupStat[] = [
    { key: "unused_measures", label: "Unused Measures", value: c.unused_measures ?? 0, accent: "danger" },
    { key: "unused_columns", label: "Unused Columns", value: c.unused_columns ?? 0, accent: "danger" },
    { key: "missing_descriptions", label: "Missing Docs", value: c.missing_descriptions ?? 0, accent: "warning" },
    { key: "attention", label: "Needs Attention", value: c.attention ?? 0, accent: "warning" },
    { key: "deprecated", label: "Deleted", value: c.deprecated ?? 0, accent: "muted" },
  ];

  const tabs: CleanupTab[] = [
    {
      kind: "query",
      key: "unused_measures",
      label: "Unused Measures",
      count: c.unused_measures,
      params: { item_type: "PB_MEASURE", is_unused: "true" },
    },
    {
      kind: "query",
      key: "unused_columns",
      label: "Unused Columns",
      count: c.unused_columns,
      params: { item_type: "PB_COLUMN", is_unused: "true" },
    },
    {
      kind: "query",
      key: "attention",
      label: "Needs Attention",
      count: c.attention,
      params: { item_group__status: "ATTENTION" },
    },
    {
      kind: "query",
      key: "deprecated",
      label: "Deleted",
      count: c.deprecated,
      deletedTab: true,
      params: { status: "DELETED", include_deleted: "true" },
    },
  ];

  return (
    <div>
      <PageHeader
        title="PowerBI Cleanup Opportunities"
        description="Unused measures and columns, assets flagged for attention, and deleted assets. Select rows and mark a group to delete — it moves to the Deleted tab where you can undo. Missing-docs is summarised in the KPI card."
      />
      <CleanupView
        service="powerbi"
        stats={stats}
        tabs={tabs}
        showFilters
        filterValues={filters}
        onFilterChange={setFilters}
        countsKey={[...countsKey]}
      />
    </div>
  );
}
