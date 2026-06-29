"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, GitBranch, Trash2, Undo2 } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { LoadingState, EmptyState, Stat } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import { api, type FiltersResponse, type ItemStatus } from "@/lib/api";
import { GROUP_LABELS, colorFor } from "@/lib/lineage/graph-utils";
import { fmtInt } from "@/lib/utils";

const PAGE_SIZE = 25;

/** One KPI card on the strip above the tabs. */
export interface CleanupStat {
  key: string;
  label: string;
  value: number;
  /** Tints the number to flag severity. */
  accent?: "danger" | "warning" | "muted";
}

/** A cleanup row — either a real Item or a dbt-insights list row. Both carry a
 * group pk (Item.group, insights item_group) and a soft-delete flag. */
export interface CleanupRow {
  item_id: string;
  item_name?: string | null;
  item_type?: string | null;
  type?: string | null;
  workspace_name?: string | null;
  dataset_name?: string | null;
  database_name?: string | null;
  schema_name?: string | null;
  status?: ItemStatus | string | null;
  /** ItemGroup pk — `group` on Items, `item_group` on insights rows. */
  group?: number | null;
  item_group?: number | null;
  deleted?: boolean | null;
}

type Loc = "powerbi" | "dbt";

interface BaseTab {
  key: string;
  label: string;
  /** Badge count (from the KPI counts), independent of the loaded rows. */
  count?: number;
  /** Show Undo (patch deleted:false) instead of a checkbox/Mark to delete. */
  deletedTab?: boolean;
}

/** Tab whose rows are fetched server-side via api.items.list. */
export interface QueryTab extends BaseTab {
  kind: "query";
  /** Extra params merged into the items.list call (item_type, status, …). */
  params: Record<string, string | number>;
}

/** Tab rendered from rows already in memory (e.g. dbtInsights lists). */
export interface RowsTab extends BaseTab {
  kind: "rows";
  rows: CleanupRow[];
}

export type CleanupTab = QueryTab | RowsTab;

export function CleanupView({
  stats,
  tabs,
  service,
  showFilters = false,
  filterValues,
  onFilterChange,
  countsKey,
}: {
  stats: CleanupStat[];
  tabs: CleanupTab[];
  service: Loc;
  /** Workspace + Dataset filter (PowerBI only). */
  showFilters?: boolean;
  filterValues?: { workspace_name: string; dataset_name: string };
  onFilterChange?: (v: { workspace_name: string; dataset_name: string }) => void;
  /** Query keys to invalidate after a mark/undo so KPI counts + lists refresh. */
  countsKey: unknown[];
}) {
  const [active, setActive] = useState(tabs[0]?.key ?? "");
  const activeTab = tabs.find((t) => t.key === active) ?? tabs[0];

  return (
    <div className="space-y-6">
      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-5 lg:grid-cols-7">
        {stats.map((s) => (
          <Stat
            key={s.key}
            label={s.label}
            value={
              <span className={accentCls(s.accent)}>{fmtInt(s.value)}</span>
            }
          />
        ))}
      </div>

      {showFilters && filterValues && onFilterChange && (
        <FilterBar value={filterValues} onChange={onFilterChange} />
      )}

      {/* Tab bar with count badges */}
      <div className="border-b border-line">
        <nav className="-mb-px flex flex-wrap gap-x-6">
          {tabs.map((t) => {
            const on = t.key === active;
            return (
              <button
                key={t.key}
                onClick={() => setActive(t.key)}
                className={[
                  "inline-flex items-center gap-2 border-b-2 py-3 text-[13px] font-semibold transition-colors",
                  on
                    ? "border-brand text-brand"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                ].join(" ")}
              >
                {t.label}
                {t.count !== undefined && (
                  <span className="rounded-full bg-panel2 px-1.5 py-0.5 text-[10px] font-semibold text-faint">
                    {fmtInt(t.count)}
                  </span>
                )}
              </button>
            );
          })}
        </nav>
      </div>

      {activeTab && (
        <CleanupTabPanel
          key={activeTab.key}
          tab={activeTab}
          service={service}
          filterValues={showFilters ? filterValues : undefined}
          countsKey={countsKey}
        />
      )}
    </div>
  );
}

function CleanupTabPanel({
  tab,
  service,
  filterValues,
  countsKey,
}: {
  tab: CleanupTab;
  service: Loc;
  filterValues?: { workspace_name: string; dataset_name: string };
  countsKey: unknown[];
}) {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);

  // ---- server-side rows (query tab) ---------------------------------------
  const params: Record<string, string | number> = { service, page };
  if (tab.kind === "query") Object.assign(params, tab.params);
  if (filterValues?.workspace_name) params.workspace_name = filterValues.workspace_name;
  if (filterValues?.dataset_name) params.dataset_name = filterValues.dataset_name;

  const queryQ = useQuery({
    queryKey: ["cleanup-items", params],
    queryFn: () => api.items.list(params),
    placeholderData: keepPreviousData,
    enabled: tab.kind === "query",
  });

  // ---- mark / undo --------------------------------------------------------
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["cleanup-items"] });
    qc.invalidateQueries({ queryKey: countsKey });
  };

  const markMut = useMutation({
    mutationFn: (group: number) => api.itemGroups.patch(group, { deleted: true }),
    onSuccess: invalidate,
    onError: () => alert("Error marking to delete. You may not have permission."),
  });

  const undoMut = useMutation({
    mutationFn: (group: number) =>
      api.itemGroups.patch(group, { deleted: false, status: "UNVERIFIED" }),
    onSuccess: invalidate,
    onError: () => alert("Error restoring. You may not have permission."),
  });

  // ---- rows ---------------------------------------------------------------
  const allRows: CleanupRow[] =
    tab.kind === "rows" ? tab.rows : ((queryQ.data?.results ?? []) as CleanupRow[]);

  const isServer = tab.kind === "query";
  const count = isServer ? queryQ.data?.count ?? 0 : allRows.length;
  const totalPages = Math.max(1, Math.ceil(count / PAGE_SIZE));

  // Server tab is already paged by the API; rows tab is paged in memory.
  const rows = useMemo(
    () => (isServer ? allRows : allRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)),
    [isServer, allRows, page],
  );

  const isLoading = tab.kind === "query" && queryQ.isLoading;
  const isError = tab.kind === "query" && queryQ.isError;

  function markRow(g: number | null | undefined) {
    if (g == null) return;
    if (!window.confirm("Mark this asset to delete? It'll move to the Deleted tab where you can undo."))
      return;
    markMut.mutate(g);
  }

  return (
    <Card className="relative overflow-hidden">
      <div className="flex items-center justify-between border-b border-line px-4 py-2.5 text-[12px] text-faint">
        <span>{fmtInt(count)} rows</span>
        {tab.kind === "query" && queryQ.isFetching && <span>Loading…</span>}
      </div>

      {isLoading && <LoadingState />}
      {isError && <EmptyState title="Failed to load" hint="The catalog API returned an error." />}
      {!isLoading && !isError && rows.length === 0 && (
        <EmptyState title="No items in this category" hint="Clean ship — nothing to do here." />
      )}

      {!isLoading && rows.length > 0 && (
        <>
          <Table>
            <THead>
              <TR>
                {service === "powerbi" ? (
                  <>
                    <TH>Workspace</TH>
                    <TH>Dataset</TH>
                  </>
                ) : (
                  <>
                    <TH>Database</TH>
                    <TH>Schema</TH>
                  </>
                )}
                <TH>Asset Name</TH>
                <TH>Type</TH>
                <TH>Status</TH>
                <TH className="text-right">Action</TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((r) => {
                const g = groupPk(r);
                const type = (r.item_type || r.type || "") as string;
                const composite = `${type.toUpperCase()}::${r.item_id}`;
                const isDeleted = !!r.deleted || r.status === "DELETED";
                return (
                  <TR key={r.item_id}>
                    {service === "powerbi" ? (
                      <>
                        <TD className="max-w-[180px] truncate text-[12.5px] text-muted-foreground">
                          {r.workspace_name || "—"}
                        </TD>
                        <TD className="max-w-[180px] truncate text-[12.5px] text-muted-foreground">
                          {r.dataset_name || "—"}
                        </TD>
                      </>
                    ) : (
                      <>
                        <TD className="max-w-[180px] truncate text-[12.5px] text-muted-foreground">
                          {r.database_name || "—"}
                        </TD>
                        <TD className="max-w-[180px] truncate text-[12.5px] text-muted-foreground">
                          {r.schema_name || "—"}
                        </TD>
                      </>
                    )}
                    <TD className="max-w-[320px]">
                      <span className="inline-flex items-center gap-1.5">
                        <span className="truncate font-medium">{r.item_name || "—"}</span>
                        <Link
                          href={`/lineage?node_id=${encodeURIComponent(composite)}`}
                          target="_blank"
                          className="shrink-0 text-faint hover:text-brand"
                          title="View in Lineage Graph"
                        >
                          <GitBranch className="h-3.5 w-3.5" />
                        </Link>
                      </span>
                    </TD>
                    <TD>
                      <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-[12px]">
                        <span
                          className="h-2 w-2 rounded-full"
                          style={{ background: colorFor(type) }}
                        />
                        {GROUP_LABELS[type] ?? type ?? "—"}
                      </span>
                    </TD>
                    <TD>
                      <StatusBadge status={r.status} />
                    </TD>
                    <TD className="text-right">
                      {tab.deletedTab || r.deleted ? (
                        g != null ? (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={undoMut.isPending}
                            onClick={() => undoMut.mutate(g)}
                          >
                            <Undo2 className="h-3.5 w-3.5" /> Undo
                          </Button>
                        ) : (
                          <span className="text-[12px] text-faint">—</span>
                        )
                      ) : isDeleted ? (
                        <span className="rounded-md border border-line-strong bg-panel2 px-2 py-1 text-[12px] text-faint">
                          Deleted
                        </span>
                      ) : g != null ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="border-err/30 text-err hover:bg-err/10 hover:text-err"
                          disabled={markMut.isPending && markMut.variables === g}
                          onClick={() => markRow(g)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {markMut.isPending && markMut.variables === g ? "Marking…" : "Mark to delete"}
                        </Button>
                      ) : (
                        <span className="text-[12px] text-faint">—</span>
                      )}
                    </TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>

          <div className="flex items-center justify-between border-t border-line px-4 py-2.5 text-[12.5px] text-muted-foreground">
            <span>
              Page {page} of {totalPages}
            </span>
            <div className="flex items-center gap-1">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="inline-flex h-8 items-center gap-1 rounded-md border border-line-strong bg-panel px-2.5 disabled:opacity-40"
              >
                <ChevronLeft className="h-3.5 w-3.5" /> Prev
              </button>
              <button
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                className="inline-flex h-8 items-center gap-1 rounded-md border border-line-strong bg-panel px-2.5 disabled:opacity-40"
              >
                Next <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </>
      )}
    </Card>
  );
}

function FilterBar({
  value,
  onChange,
}: {
  value: { workspace_name: string; dataset_name: string };
  onChange: (v: { workspace_name: string; dataset_name: string }) => void;
}) {
  // Datasets cascade off the selected workspace (dependent filters).
  const filtersQ = useQuery<FiltersResponse>({
    queryKey: ["cleanup-filters", value.workspace_name],
    queryFn: () => api.filters({ workspace_name: value.workspace_name }),
    staleTime: 5 * 60_000,
  });
  return (
    <Card className="flex flex-wrap items-end gap-4 p-4">
      <div>
        <label className="mb-1 block text-[11px] font-medium text-faint">Workspace</label>
        <SimpleSelect
          value={value.workspace_name}
          onValueChange={(v) => onChange({ workspace_name: v, dataset_name: "" })}
          className="min-w-[160px]"
          options={[
            { value: "", label: "All Workspaces" },
            ...(filtersQ.data?.workspaces ?? []).map((w) => ({ value: w, label: w })),
          ]}
        />
      </div>
      <div>
        <label className="mb-1 block text-[11px] font-medium text-faint">Dataset</label>
        <SimpleSelect
          value={value.dataset_name}
          onValueChange={(v) => onChange({ ...value, dataset_name: v })}
          className="min-w-[160px]"
          options={[
            { value: "", label: "All Datasets" },
            ...(filtersQ.data?.datasets ?? []).map((d) => ({ value: d, label: d })),
          ]}
        />
      </div>
    </Card>
  );
}

function StatusBadge({ status }: { status?: ItemStatus | string | null }) {
  const s = (status || "UNVERIFIED") as string;
  const variant =
    s === "VERIFIED"
      ? "success"
      : s === "ATTENTION"
        ? "danger"
        : s === "DELETED"
          ? "outline"
          : "warning";
  return <Badge variant={variant}>{s}</Badge>;
}

function accentCls(accent?: CleanupStat["accent"]): string {
  if (accent === "danger") return "text-err";
  if (accent === "warning") return "text-warn";
  if (accent === "muted") return "text-muted-foreground";
  return "";
}

/** Group pk lives on `group` (Items) or `item_group` (insights rows). */
function groupPk(r: CleanupRow): number | null {
  if (r.group != null) return r.group;
  if (r.item_group != null) return r.item_group;
  return null;
}

/** Narrow a loosely-typed insights list (unknown[]) into CleanupRow[]. */
export function asCleanupRows(rows: unknown): CleanupRow[] {
  if (!Array.isArray(rows)) return [];
  return rows as CleanupRow[];
}
