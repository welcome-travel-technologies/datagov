"use client";

import { useState } from "react";
import Link from "next/link";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Search, GitBranch, ChevronLeft, ChevronRight, ArrowUpDown } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import { NodeDetailPanel } from "@/components/lineage/node-detail-panel";
import { api, type Item } from "@/lib/api";
import { fmtInt } from "@/lib/utils";
import { GROUP_LABELS, colorFor } from "@/lib/lineage/graph-utils";
import { useDebounced } from "@/lib/use-debounced";

export interface CatalogColumn {
  key: keyof Item & string;
  label: string;
  sortable?: boolean;
}

const FILTER_TRIGGER_CLS = "w-auto min-w-[150px]";

const STATUS_FILTER_OPTIONS = [
  { value: "UNVERIFIED", label: "Unverified" },
  { value: "VERIFIED", label: "Verified" },
  { value: "ATTENTION", label: "Attention" },
  { value: "DELETED", label: "Deleted" },
];

export function CatalogView({
  baseParams = {},
  typeOptions,
  columns,
  defaultOrdering,
  emptyHint,
  pageSize = 50,
  kpis,
  showWorkspaceFilter,
  showDatasetFilter,
  showStatusFilter,
  showUsageFilter,
}: {
  baseParams?: Record<string, string | number>;
  typeOptions?: { value: string; label: string }[];
  columns: CatalogColumn[];
  defaultOrdering?: string;
  emptyHint?: string;
  pageSize?: number;
  kpis?: React.ReactNode;
  showWorkspaceFilter?: boolean;
  showDatasetFilter?: boolean;
  showStatusFilter?: boolean;
  showUsageFilter?: boolean;
}) {
  const [searchInput, setSearchInput] = useState("");
  const search = useDebounced(searchInput, 350);
  const [itemType, setItemType] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [dataset, setDataset] = useState("");
  const [status, setStatus] = useState("");
  const [usage, setUsage] = useState("");
  const [page, setPage] = useState(1);
  const [ordering, setOrdering] = useState(defaultOrdering ?? "");
  const [detail, setDetail] = useState<{ nodeId: string; label: string } | null>(null);

  const needsFilters = !!(showWorkspaceFilter || showDatasetFilter);
  // Datasets cascade off the selected workspace (dependent filters).
  const { data: filterOpts } = useQuery({
    queryKey: ["catalog-filters", workspace],
    queryFn: () => api.filters({ workspace_name: workspace }),
    staleTime: 5 * 60_000,
    enabled: needsFilters,
  });

  const params: Record<string, string | number> = { ...baseParams, page };
  if (search) params.search = search;
  if (itemType) params.item_type = itemType;
  if (workspace) params.workspace_name = workspace;
  if (dataset) params.dataset_name = dataset;
  if (status) params.status = status;
  if (usage) params.is_unused = usage; // "true" | "false"
  if (ordering) params.ordering = ordering;

  const { data, isLoading, isError } = useQuery({
    queryKey: ["items", params],
    queryFn: () => api.items.list(params),
    placeholderData: keepPreviousData,
  });

  const rows = data?.results ?? [];
  const count = data?.count ?? 0;
  const totalPages = Math.max(1, Math.ceil(count / pageSize));

  function toggleSort(key: string) {
    setPage(1);
    setOrdering((cur) => (cur === `-${key}` ? key : `-${key}`));
  }

  return (
    <>
      {kpis}
      <Card className="overflow-hidden">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-3 border-b border-line p-4">
        <div className="flex h-9 min-w-[260px] flex-1 items-center gap-2 rounded-md border border-input bg-panel px-3 text-[13px]">
          <Search className="h-3.5 w-3.5 text-faint" />
          <input
            value={searchInput}
            onChange={(e) => {
              setSearchInput(e.target.value);
              setPage(1);
            }}
            placeholder="Search by name…"
            className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-faint"
          />
        </div>
        {typeOptions && (
          <SimpleSelect
            value={itemType}
            onValueChange={(v) => { setItemType(v); setPage(1); }}
            className={FILTER_TRIGGER_CLS}
            options={[{ value: "", label: "All types" }, ...typeOptions]}
          />
        )}
        {showWorkspaceFilter && (
          <SimpleSelect
            value={workspace}
            onValueChange={(v) => { setWorkspace(v); setDataset(""); setPage(1); }}
            className={FILTER_TRIGGER_CLS}
            options={[
              { value: "", label: "All workspaces" },
              ...(filterOpts?.workspaces ?? []).map((w) => ({ value: w, label: w })),
            ]}
          />
        )}
        {showDatasetFilter && (
          <SimpleSelect
            value={dataset}
            onValueChange={(v) => { setDataset(v); setPage(1); }}
            className={FILTER_TRIGGER_CLS}
            options={[
              { value: "", label: "All datasets" },
              ...(filterOpts?.datasets ?? []).map((d) => ({ value: d, label: d })),
            ]}
          />
        )}
        {showStatusFilter && (
          <SimpleSelect
            value={status}
            onValueChange={(v) => { setStatus(v); setPage(1); }}
            className={FILTER_TRIGGER_CLS}
            options={[{ value: "", label: "All statuses" }, ...STATUS_FILTER_OPTIONS]}
          />
        )}
        {showUsageFilter && (
          <SimpleSelect
            value={usage}
            onValueChange={(v) => { setUsage(v); setPage(1); }}
            className={FILTER_TRIGGER_CLS}
            options={[
              { value: "", label: "All usage" },
              { value: "false", label: "Used" },
              { value: "true", label: "Unused" },
            ]}
          />
        )}
        <span className="text-[12px] text-faint">{fmtInt(count)} items</span>
      </div>

      {isLoading && <LoadingState />}
      {isError && <EmptyState title="Failed to load items" hint="The catalog API returned an error." />}
      {!isLoading && !isError && rows.length === 0 && (
        <EmptyState title="No items found" hint={emptyHint ?? "Try a different search or filter."} />
      )}

      {!isLoading && rows.length > 0 && (
        <>
          <Table>
            <THead>
              <TR>
                <TH>Name</TH>
                <TH>Type</TH>
                <TH>Workspace</TH>
                {columns.map((c) => (
                  <TH key={c.key} className={c.sortable ? "cursor-pointer select-none" : ""}>
                    {c.sortable ? (
                      <button className="inline-flex items-center gap-1" onClick={() => toggleSort(c.key)}>
                        {c.label}
                        <ArrowUpDown className="h-3 w-3 opacity-50" />
                      </button>
                    ) : (
                      c.label
                    )}
                  </TH>
                ))}
                <TH className="text-right">Lineage</TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((it) => (
                <TR key={it.item_id} className="cursor-pointer" onClick={() => setDetail({ nodeId: it.item_id, label: it.item_name })}>
                  <TD className="max-w-[320px]">
                    <span className="block truncate font-medium">{it.item_name}</span>
                    {it.dataset_name && <span className="block truncate text-[11px] text-faint">{it.dataset_name}</span>}
                  </TD>
                  <TD>
                    <span className="inline-flex items-center gap-1.5 text-[12px]">
                      <span className="h-2 w-2 rounded-full" style={{ background: colorFor(it.item_type) }} />
                      {GROUP_LABELS[it.item_type] ?? it.item_type}
                    </span>
                  </TD>
                  <TD className="max-w-[180px] truncate text-[12.5px] text-muted-foreground">{it.workspace_name ?? "—"}</TD>
                  {columns.map((c) => (
                    <TD key={c.key} className="text-[13px]">
                      {renderCell(it, c.key)}
                    </TD>
                  ))}
                  <TD className="text-right">
                    <Link
                      href={`/lineage?node_id=${encodeURIComponent(it.item_id)}&mode=column`}
                      onClick={(e) => e.stopPropagation()}
                      className="inline-flex items-center gap-1 text-brand hover:underline"
                    >
                      <GitBranch className="h-3.5 w-3.5" />
                    </Link>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>

          <div className="flex items-center justify-between border-t border-line px-4 py-2.5 text-[12.5px] text-muted-foreground">
            <span>
              Page {page} of {totalPages}
            </span>
            <div className="flex items-center gap-1">
              <button
                disabled={!data?.previous}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="inline-flex h-8 items-center gap-1 rounded-md border border-line-strong bg-panel px-2.5 disabled:opacity-40"
              >
                <ChevronLeft className="h-3.5 w-3.5" /> Prev
              </button>
              <button
                disabled={!data?.next}
                onClick={() => setPage((p) => p + 1)}
                className="inline-flex h-8 items-center gap-1 rounded-md border border-line-strong bg-panel px-2.5 disabled:opacity-40"
              >
                Next <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </>
      )}

      <NodeDetailPanel nodeId={detail?.nodeId ?? null} label={detail?.label ?? ""} onClose={() => setDetail(null)} />
      </Card>
    </>
  );
}

function renderCell(it: Item, key: string) {
  if (key === "is_unused") {
    return it.is_unused ? <Badge variant="danger">Unused</Badge> : <Badge variant="success">In use</Badge>;
  }
  const v = (it as Record<string, unknown>)[key];
  if (typeof v === "number") return fmtInt(v);
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}
