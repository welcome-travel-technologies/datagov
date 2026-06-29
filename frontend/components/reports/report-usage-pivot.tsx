"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import { api } from "@/lib/api";
import { fmtInt } from "@/lib/utils";

/** Dimensions the pivot can be reshaped by (subset of the usage whitelist). */
const PIVOT_DIMS = [
  { key: "report_name", label: "Report" },
  { key: "workspace_name", label: "Workspace" },
  { key: "platform", label: "Platform" },
  { key: "distribution_method", label: "Distribution" },
  { key: "month", label: "Month" },
  { key: "report_page", label: "Page" },
] as const;

type DimKey = (typeof PIVOT_DIMS)[number]["key"];
const DIM_LABEL = Object.fromEntries(PIVOT_DIMS.map((d) => [d.key, d.label])) as Record<DimKey, string>;

const MEASURES = [
  { key: "view_count", label: "Total Views" },
  { key: "unique_users", label: "Unique Users" },
] as const;
type MeasureKey = (typeof MEASURES)[number]["key"];

const SELECT_TRIGGER_CLS = "min-w-[150px]";

function dimDisplay(dim: string, v: unknown): string {
  if (v == null || v === "") return "—";
  if (dim === "month") return String(v).substring(0, 7);
  return String(v);
}

export function ReportUsagePivot({
  workspace,
  month,
  monthsReady,
  pivotY,
  pivotX,
  measure,
  onChangeY,
  onChangeX,
  onChangeMeasure,
}: {
  workspace: string;
  month: string;
  /** Gate the first fetch on the Month dropdown being populated (champions pattern). */
  monthsReady: boolean;
  pivotY: DimKey;
  pivotX: DimKey;
  measure: MeasureKey;
  onChangeY: (v: DimKey) => void;
  onChangeX: (v: DimKey) => void;
  onChangeMeasure: (v: MeasureKey) => void;
}) {
  // For Unique Users, summing per-cell counts over-counts repeat viewers, so we
  // always pull user_email into the group_by and dedup with Sets at every axis
  // (mirrors the original pb_reports template's roll-up strategy).
  const apiGroupBy = useMemo(() => {
    const dims = new Set<string>([pivotY, pivotX]);
    if (measure === "unique_users") dims.add("user_email");
    return Array.from(dims).join(",");
  }, [pivotY, pivotX, measure]);

  const usageQ = useQuery({
    queryKey: ["report-usage-pivot", apiGroupBy, workspace, month],
    enabled: monthsReady,
    queryFn: () =>
      api.powerbiUsage({
        group_by: apiGroupBy,
        workspace_name: workspace || undefined,
        month: month || undefined,
        limit: 50000,
      }),
    staleTime: 30_000,
  });

  const pivot = useMemo(() => {
    const rows = usageQ.data?.results ?? [];
    const distinct = measure === "unique_users";

    // For count measures we accumulate numbers; for unique_users we accumulate
    // Sets of user_email and read their size at render time.
    const cell: Record<string, number | Set<string>> = {};
    const rowTot: Record<string, number | Set<string>> = {};
    const colTot: Record<string, number | Set<string>> = {};
    let grand: number | Set<string> = distinct ? new Set<string>() : 0;
    const rowSet = new Set<string>();
    const colSet = new Set<string>();

    const bumpNum = (m: Record<string, number>, k: string, n: number) => {
      m[k] = (m[k] || 0) + n;
    };
    const bumpSet = (m: Record<string, Set<string>>, k: string, u: string) => {
      (m[k] = (m[k] as Set<string>) || new Set<string>()).add(u);
    };

    for (const r of rows) {
      const y = dimDisplay(pivotY, r[pivotY]);
      const x = dimDisplay(pivotX, r[pivotX]);
      rowSet.add(y);
      colSet.add(x);
      const k = y + "||" + x;
      if (distinct) {
        const u = r.user_email || "";
        if (!u) continue;
        bumpSet(cell as Record<string, Set<string>>, k, u);
        bumpSet(rowTot as Record<string, Set<string>>, y, u);
        bumpSet(colTot as Record<string, Set<string>>, x, u);
        (grand as Set<string>).add(u);
      } else {
        const n = r.view_count || 0;
        bumpNum(cell as Record<string, number>, k, n);
        bumpNum(rowTot as Record<string, number>, y, n);
        bumpNum(colTot as Record<string, number>, x, n);
        grand = (grand as number) + n;
      }
    }

    const val = (v: number | Set<string> | undefined): number =>
      v === undefined ? 0 : typeof v === "number" ? v : v.size;

    const cellVal: Record<string, number> = {};
    for (const [k, v] of Object.entries(cell)) cellVal[k] = val(v);
    const rowVal: Record<string, number> = {};
    for (const [k, v] of Object.entries(rowTot)) rowVal[k] = val(v);
    const colVal: Record<string, number> = {};
    for (const [k, v] of Object.entries(colTot)) colVal[k] = val(v);

    // Sort axes by their total descending, pushing "—" (missing) to the end.
    const sortAxis = (keys: string[], totals: Record<string, number>) =>
      keys.sort((a, b) => {
        if (a === "—" && b !== "—") return 1;
        if (b === "—" && a !== "—") return -1;
        return (totals[b] || 0) - (totals[a] || 0) || a.localeCompare(b);
      });

    return {
      rows: sortAxis([...rowSet], rowVal),
      cols: sortAxis([...colSet], colVal),
      cell: cellVal,
      rowTot: rowVal,
      colTot: colVal,
      grand: val(grand),
    };
  }, [usageQ.data, pivotY, pivotX, measure]);

  const measureLabel = MEASURES.find((m) => m.key === measure)?.label ?? "";

  return (
    <div>
      <div className="flex flex-wrap items-end gap-3 border-b border-line bg-panel2/40 p-4">
        <PivotSelect
          label="Rows (Y axis)"
          value={pivotY}
          onChange={(v) => onChangeY(v as DimKey)}
          options={PIVOT_DIMS.map((d) => ({ value: d.key, label: d.label }))}
        />
        <PivotSelect
          label="Columns (X axis)"
          value={pivotX}
          onChange={(v) => onChangeX(v as DimKey)}
          options={PIVOT_DIMS.map((d) => ({ value: d.key, label: d.label }))}
        />
        <PivotSelect
          label="Measure"
          value={measure}
          onChange={(v) => onChangeMeasure(v as MeasureKey)}
          options={MEASURES.map((m) => ({ value: m.key, label: m.label }))}
        />
        <span className="self-center text-[11px] text-faint">
          {DIM_LABEL[pivotY]} × {DIM_LABEL[pivotX]} · {measureLabel}
        </span>
      </div>

      {usageQ.isLoading || !monthsReady ? (
        <LoadingState label="Pivoting report views…" />
      ) : usageQ.isError ? (
        <EmptyState title="Failed to load usage" hint="Try a different workspace or month." />
      ) : pivot.rows.length === 0 ? (
        <EmptyState title="No usage in this period" hint="Pick a different workspace or month." />
      ) : (
        <div className="overflow-x-auto p-4">
          <table className="w-full min-w-[640px] text-left text-[12px]">
            <thead className="text-faint">
              <tr className="border-b border-line">
                <th className="px-2.5 py-2 font-semibold uppercase tracking-[0.04em]">
                  {DIM_LABEL[pivotY]}
                </th>
                {pivot.cols.map((c) => (
                  <th key={c} className="px-2.5 py-2 text-center font-semibold" title={c}>
                    {c}
                  </th>
                ))}
                <th className="bg-panel2 px-2.5 py-2 text-center font-semibold">Total</th>
              </tr>
            </thead>
            <tbody>
              {pivot.rows.map((rk) => (
                <tr key={rk} className="border-b border-line hover:bg-panel2/60">
                  <td className="px-2.5 py-2 font-medium" title={rk}>{rk}</td>
                  {pivot.cols.map((ck) => {
                    const v = pivot.cell[rk + "||" + ck] || 0;
                    return (
                      <td key={ck} className="px-2.5 py-2 text-center">
                        {v ? <Badge variant="info">{fmtInt(v)}</Badge> : <span className="text-faint">–</span>}
                      </td>
                    );
                  })}
                  <td className="bg-panel2 px-2.5 py-2 text-center font-semibold">
                    {pivot.rowTot[rk] ? fmtInt(pivot.rowTot[rk]) : "–"}
                  </td>
                </tr>
              ))}
              <tr className="bg-panel2 font-semibold">
                <td className="px-2.5 py-2">Total</td>
                {pivot.cols.map((ck) => (
                  <td key={ck} className="px-2.5 py-2 text-center">
                    {pivot.colTot[ck] ? fmtInt(pivot.colTot[ck]) : "–"}
                  </td>
                ))}
                <td className="px-2.5 py-2 text-center">{pivot.grand ? fmtInt(pivot.grand) : "–"}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PivotSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <div>
      <label className="mb-1 block text-[11px] font-medium text-faint">{label}</label>
      <SimpleSelect value={value} onValueChange={onChange} options={options} className={SELECT_TRIGGER_CLS} />
    </div>
  );
}

export type { DimKey, MeasureKey };
