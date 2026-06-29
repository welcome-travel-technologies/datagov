"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpDown, ExternalLink } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { api, type Item } from "@/lib/api";
import { fmtInt } from "@/lib/utils";

/** A distilled report-health row with its complexity score precomputed. */
interface HealthRow {
  id: string;
  name: string;
  url: string | null;
  workspace: string;
  pages: number;
  visuals: number;
  measures: number;
  columns: number;
  complexity: number; // visuals + measures — the "weight" of the report
  ratio: number; // visuals per page — drives the health band
}

type SortKey = "name" | "workspace" | "pages" | "visuals" | "measures" | "columns" | "complexity";

const NUMERIC: Record<SortKey, boolean> = {
  name: false,
  workspace: false,
  pages: true,
  visuals: true,
  measures: true,
  columns: true,
  complexity: true,
};

function healthBadge(ratio: number) {
  // Visuals-per-page banding (matches the original template thresholds).
  if (ratio > 20) return <Badge variant="danger">Complex ({ratio.toFixed(1)} v/p)</Badge>;
  if (ratio > 10) return <Badge variant="warning">Moderate ({ratio.toFixed(1)} v/p)</Badge>;
  return <Badge variant="success">Healthy ({ratio.toFixed(1)} v/p)</Badge>;
}

export function ReportHealthTable({ workspace }: { workspace: string }) {
  const [sortKey, setSortKey] = useState<SortKey>("complexity");
  const [desc, setDesc] = useState(true);

  const reportsQ = useQuery({
    queryKey: ["report-health"],
    queryFn: () => api.items.list({ item_type: "PB_REPORT", limit: 100000 }),
    staleTime: 60_000,
  });

  const rows = useMemo<HealthRow[]>(() => {
    const items: Item[] = reportsQ.data?.results ?? [];
    const filtered = workspace ? items.filter((it) => (it.workspace_name || "") === workspace) : items;
    return filtered.map((it) => {
      const pages = it.connected_report_pages || 0;
      const visuals = it.connected_visuals || 0;
      const measures = it.connected_measures || 0;
      const columns = it.connected_columns || 0;
      return {
        id: it.item_id,
        name: it.item_name || "Unknown",
        url: it.web_url ?? null,
        workspace: it.workspace_name || "—",
        pages,
        visuals,
        measures,
        columns,
        complexity: visuals + measures,
        ratio: visuals / (pages || 1),
      };
    });
  }, [reportsQ.data, workspace]);

  const sorted = useMemo(() => {
    const out = [...rows];
    out.sort((a, b) => {
      let cmp: number;
      if (NUMERIC[sortKey]) cmp = (a[sortKey] as number) - (b[sortKey] as number);
      else cmp = String(a[sortKey]).localeCompare(String(b[sortKey]));
      return desc ? -cmp : cmp;
    });
    return out;
  }, [rows, sortKey, desc]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) setDesc((d) => !d);
    else {
      setSortKey(key);
      setDesc(NUMERIC[key]); // numbers default to desc, text to asc
    }
  }

  if (reportsQ.isLoading) return <LoadingState label="Scoring report complexity…" />;
  if (reportsQ.isError) return <EmptyState title="Failed to load reports" hint="Try refreshing." />;
  if (sorted.length === 0)
    return <EmptyState title="No PowerBI reports found" hint="Pick a different workspace." />;

  return (
    <Table>
      <THead>
        <TR>
          <SortableTH label="Report Name" col="name" sortKey={sortKey} desc={desc} onSort={toggleSort} />
          <SortableTH label="Workspace" col="workspace" sortKey={sortKey} desc={desc} onSort={toggleSort} />
          <SortableTH label="Pages" col="pages" sortKey={sortKey} desc={desc} onSort={toggleSort} center />
          <SortableTH label="Visuals" col="visuals" sortKey={sortKey} desc={desc} onSort={toggleSort} center />
          <SortableTH label="Measures" col="measures" sortKey={sortKey} desc={desc} onSort={toggleSort} center />
          <SortableTH label="Columns" col="columns" sortKey={sortKey} desc={desc} onSort={toggleSort} center />
          <SortableTH label="Complexity" col="complexity" sortKey={sortKey} desc={desc} onSort={toggleSort} center />
          <TH className="text-center">Health</TH>
        </TR>
      </THead>
      <TBody>
        {sorted.map((r) => (
          <TR key={r.id}>
            <TD className="max-w-[320px]">
              {r.url ? (
                <a
                  href={r.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 font-medium text-brand hover:underline"
                >
                  <span className="truncate">{r.name}</span>
                  <ExternalLink className="h-3 w-3 shrink-0 opacity-70" />
                </a>
              ) : (
                <span className="block truncate font-medium">{r.name}</span>
              )}
            </TD>
            <TD className="max-w-[180px] truncate text-[12.5px] text-muted-foreground">{r.workspace}</TD>
            <TD className="text-center">{fmtInt(r.pages)}</TD>
            <TD className="text-center font-medium">{fmtInt(r.visuals)}</TD>
            <TD className="text-center">{fmtInt(r.measures)}</TD>
            <TD className="text-center">{fmtInt(r.columns)}</TD>
            <TD className="text-center font-semibold">{fmtInt(r.complexity)}</TD>
            <TD className="text-center">{healthBadge(r.ratio)}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function SortableTH({
  label,
  col,
  sortKey,
  desc,
  onSort,
  center,
}: {
  label: string;
  col: SortKey;
  sortKey: SortKey;
  desc: boolean;
  onSort: (k: SortKey) => void;
  center?: boolean;
}) {
  const active = sortKey === col;
  return (
    <TH className={`cursor-pointer select-none ${center ? "text-center" : ""}`}>
      <button
        className={`inline-flex items-center gap-1 ${center ? "justify-center" : ""} ${active ? "text-foreground" : ""}`}
        onClick={() => onSort(col)}
      >
        {label}
        <ArrowUpDown className={`h-3 w-3 ${active ? "opacity-90" : "opacity-40"}`} />
        {active && <span className="text-[9px]">{desc ? "▼" : "▲"}</span>}
      </button>
    </TH>
  );
}
