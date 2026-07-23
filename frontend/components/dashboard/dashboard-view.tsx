"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, Database, Eye } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { LoadingState } from "@/components/ui/misc";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { api, type DataPerson } from "@/lib/api";
import { fmtInt } from "@/lib/utils";
import { SimpleSelect, type SelectOption } from "@/components/ui/simple-select";

const STATUS_LABELS: Record<string, string> = {
  UNVERIFIED: "Unverified",
  VERIFIED: "Verified",
  DELETED: "Deleted",
  ATTENTION: "Attention",
};

/** One distilled measure-group record for the KPI table + pivot. */
interface MG {
  w: string; // workspace
  di: number | null; dn: string; // department
  oi: number | null; on: string; // owner
  si: number | null; sn: string; // steward
  st: string; stn: string; // status
  ci: number | null; cn: string; // category
  h: number; // has description (0/1)
  u: number; // unused (0/1)
}

interface WsStat { r: number; p: number; v30: number; vt: number }

function pct(part: number, total: number): number {
  return total ? Math.round((part * 100) / total) : 0;
}

export function DashboardView() {
  const [dept, setDept] = useState("");
  const [owner, setOwner] = useState("");
  const [steward, setSteward] = useState("");
  const [statusF, setStatusF] = useState("");
  const [category, setCategory] = useState("");
  const [pivotY, setPivotY] = useState("department");
  const [pivotX, setPivotX] = useState("workspace");
  const [pivotMeasure, setPivotMeasure] = useState("total");

  // ---- data (single precomputed endpoint) --------------------------------
  // One request, aggregated server-side (mirrors the legacy dashboard compute)
  // instead of fetching every measure/report/page + usage and grouping in JS.
  const dashQ = useQuery({ queryKey: ["dashboard"], queryFn: api.dashboard, staleTime: 60_000 });

  const departments = dashQ.data?.departments ?? [];
  const owners = dashQ.data?.owners ?? [];
  const stewards = dashQ.data?.stewards ?? [];
  const categories = dashQ.data?.categories ?? [];

  const groups: MG[] = dashQ.data?.measure_groups ?? [];
  const wsStats: Record<string, WsStat> = dashQ.data?.ws_stats ?? {};

  // ---- KPI cards ----------------------------------------------------------
  const recentMonthLabel = dashQ.data?.summary.recent_month_label ?? "";
  const totalReports = dashQ.data?.summary.total_reports ?? 0;
  const distinctMeasures = dashQ.data?.summary.distinct_measures ?? 0;
  const unusedMeasures = dashQ.data?.summary.unused_measures_total ?? 0;
  const viewsRecentAll = dashQ.data?.summary.views_recent_all ?? 0;
  const viewsTotalAll = dashQ.data?.summary.views_total_all ?? 0;

  // ---- governance filtering ----------------------------------------------
  const anyFilter = !!(dept || owner || steward || statusF || category);
  function govOk(sel: string, id: number | null): boolean {
    if (!sel) return true;
    if (sel === "none") return id === null;
    return String(id) === sel;
  }
  const filteredGroups = useMemo(
    () =>
      groups.filter(
        (g) =>
          govOk(dept, g.di) &&
          govOk(owner, g.oi) &&
          govOk(steward, g.si) &&
          (!statusF || g.st === statusF) &&
          govOk(category, g.ci),
      ),
    [groups, dept, owner, steward, statusF, category],
  );

  // Dept-narrowed owner/steward filter options.
  const narrow = (pool: DataPerson[]) => {
    if (!dept || dept === "none") return pool;
    const idNum = Number(dept);
    return pool.filter((p) => Array.isArray(p.departments) && p.departments.includes(idNum));
  };

  // ---- workspace KPI rows -------------------------------------------------
  const drilldown = useMemo(() => {
    const rows: Record<
      string,
      { tm: number; noDesc: number; unused: number; verified: number; noOwner: number; noSteward: number; owners: Record<string, number>; stewards: Record<string, number> }
    > = {};
    const ensure = (w: string) =>
      (rows[w] = rows[w] || { tm: 0, noDesc: 0, unused: 0, verified: 0, noOwner: 0, noSteward: 0, owners: {}, stewards: {} });
    for (const g of filteredGroups) {
      const r = ensure(g.w);
      r.tm += 1;
      if (!g.h) r.noDesc += 1;
      if (g.u) r.unused += 1;
      if (g.st === "VERIFIED") r.verified += 1;
      if (g.oi === null) r.noOwner += 1;
      else r.owners[g.on] = (r.owners[g.on] || 0) + 1;
      if (g.si === null) r.noSteward += 1;
      else r.stewards[g.sn] = (r.stewards[g.sn] || 0) + 1;
    }
    if (!anyFilter) for (const w of Object.keys(wsStats)) ensure(w);
    return Object.keys(rows)
      .sort((a, b) => a.localeCompare(b))
      .map((w) => ({ w, ...rows[w], st: wsStats[w] || { r: 0, p: 0, v30: 0, vt: 0 } }));
  }, [filteredGroups, anyFilter, wsStats]);

  // ---- pivot --------------------------------------------------------------
  const FIELD: Record<string, keyof MG> = {
    department: "dn",
    owner: "on",
    steward: "sn",
    workspace: "w",
    status: "stn",
    category: "cn",
  };
  const pivot = useMemo(() => {
    const yf = FIELD[pivotY];
    const xf = FIELD[pivotX];
    const asPct = pivotMeasure === "with_desc";
    const tot: Record<string, number> = {};
    const desc: Record<string, number> = {};
    const rowTot: Record<string, number> = {};
    const rowDesc: Record<string, number> = {};
    const colTot: Record<string, number> = {};
    const colDesc: Record<string, number> = {};
    let grandTot = 0;
    let grandDesc = 0;
    const rowSet = new Set<string>();
    const colSet = new Set<string>();
    for (const g of filteredGroups) {
      const y = String(g[yf]);
      const x = String(g[xf]);
      rowSet.add(y);
      colSet.add(x);
      const k = y + " " + x;
      tot[k] = (tot[k] || 0) + 1;
      desc[k] = (desc[k] || 0) + g.h;
      rowTot[y] = (rowTot[y] || 0) + 1;
      rowDesc[y] = (rowDesc[y] || 0) + g.h;
      colTot[x] = (colTot[x] || 0) + 1;
      colDesc[x] = (colDesc[x] || 0) + g.h;
      grandTot += 1;
      grandDesc += g.h;
    }
    const sortKeys = (keys: string[]) =>
      keys.sort((a, b) => {
        const na = a.indexOf("No ") === 0;
        const nb = b.indexOf("No ") === 0;
        if (na !== nb) return na ? 1 : -1;
        return a.localeCompare(b);
      });
    return {
      rows: sortKeys([...rowSet]),
      cols: sortKeys([...colSet]),
      tot,
      desc,
      rowTot,
      rowDesc,
      colTot,
      colDesc,
      grandTot,
      grandDesc,
      asPct,
    };
  }, [filteredGroups, pivotY, pivotX, pivotMeasure]);

  const loading = dashQ.isLoading;

  if (loading) return <LoadingState label="Building your governance overview…" />;

  return (
    <div className="space-y-8">
      {/* KPI cards */}
      <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
        <Link href="/powerbi/catalog">
          <Card className="p-5 transition-shadow hover:shadow-lg">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">Total Reports</span>
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-info/10 text-info">
                <BarChart3 className="h-4 w-4" />
              </span>
            </div>
            <div className="mt-3 text-3xl font-semibold">{fmtInt(totalReports)}</div>
            <div className="mt-3 border-t border-line pt-3 text-[12px] text-muted-foreground">Active in workspace</div>
          </Card>
        </Link>
        <Link href="/powerbi/catalog">
          <Card className="p-5 transition-shadow hover:shadow-lg">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">Total Measures</span>
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-brand/10 text-brand">
                <Database className="h-4 w-4" />
              </span>
            </div>
            <div className="mt-3 text-3xl font-semibold">{fmtInt(distinctMeasures)}</div>
            <div className="mt-3 border-t border-line pt-3 text-[12px] text-muted-foreground">
              <span className="font-bold text-err">{fmtInt(unusedMeasures)}</span> unused (Bloat) · distinct (grouped)
            </div>
          </Card>
        </Link>
        <Link href="/powerbi/reports">
          <Card className="p-5 transition-shadow hover:shadow-lg">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">
                Report Views{recentMonthLabel ? ` (${recentMonthLabel})` : ""}
              </span>
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-run/10 text-run">
                <Eye className="h-4 w-4" />
              </span>
            </div>
            <div className="mt-3 text-3xl font-semibold">{fmtInt(viewsRecentAll)}</div>
            <div className="mt-3 border-t border-line pt-3 text-[12px] text-muted-foreground">
              <span className="font-bold text-run">{fmtInt(viewsTotalAll)}</span> views total (all months)
            </div>
          </Card>
        </Link>
      </div>

      {/* Workspace KPIs */}
      <Card className="overflow-hidden">
        <div className="border-b border-line p-5">
          <h2 className="text-[16px] font-semibold">Workspace KPIs</h2>
          <div className="mt-4 flex flex-wrap items-end gap-3">
            <GovSelect
              label="Department"
              value={dept}
              onChange={(v) => { setDept(v); setOwner(""); setSteward(""); }}
              options={[
                { value: "", label: "All Departments" },
                { value: "none", label: "No Department" },
                ...departments.map((d) => ({ value: String(d.id), label: d.name })),
              ]}
            />
            <GovSelect
              label="Owner"
              value={owner}
              onChange={setOwner}
              options={[
                { value: "", label: "All Owners" },
                { value: "none", label: "No Owner" },
                ...narrow(owners).map((p) => ({ value: String(p.id), label: p.name })),
              ]}
            />
            <GovSelect
              label="Steward"
              value={steward}
              onChange={setSteward}
              options={[
                { value: "", label: "All Stewards" },
                { value: "none", label: "No Steward" },
                ...narrow(stewards).map((p) => ({ value: String(p.id), label: p.name })),
              ]}
            />
            <GovSelect
              label="Status"
              value={statusF}
              onChange={setStatusF}
              options={[
                { value: "", label: "All Statuses" },
                ...Object.entries(STATUS_LABELS).map(([code, label]) => ({ value: code, label })),
              ]}
            />
            <GovSelect
              label="Category"
              value={category}
              onChange={setCategory}
              options={[
                { value: "", label: "All Categories" },
                { value: "none", label: "No Category" },
                ...categories.map((c) => ({ value: String(c.id), label: c.name })),
              ]}
            />
            {anyFilter && (
              <div className="flex items-center gap-3">
                <button
                  onClick={() => { setDept(""); setOwner(""); setSteward(""); setStatusF(""); setCategory(""); }}
                  className="inline-flex h-9 items-center rounded-md border border-line-strong bg-panel2 px-3 text-[13px] font-medium text-muted-foreground hover:bg-panel"
                >
                  Clear filters
                </button>
                <span className="text-[11px] text-faint">Filters apply to measures; reports / pages / views stay workspace totals.</span>
              </div>
            )}
          </div>
        </div>
        <Table>
          <THead>
            <TR>
              <TH>Workspace</TH>
              <TH className="text-center">Reports</TH>
              <TH className="text-center">Pages</TH>
              <TH className="text-center">Views 30d</TH>
              <TH className="text-center">Views Total</TH>
              <TH className="text-center">Measures</TH>
              <TH className="text-center">Verified %</TH>
              <TH className="text-center">With Desc</TH>
              <TH className="text-center">Unused</TH>
              <TH className="text-center">Owners</TH>
              <TH className="text-center">Owner %</TH>
              <TH className="text-center">Stewards</TH>
              <TH className="text-center">Steward %</TH>
            </TR>
          </THead>
          <TBody>
            {drilldown.length === 0 ? (
              <TR><TD colSpan={13} className="py-6 text-center italic text-faint">No workspace data available.</TD></TR>
            ) : (
              drilldown.map((r) => {
                const ownersCount = Object.keys(r.owners).length;
                const stewardsCount = Object.keys(r.stewards).length;
                return (
                  <TR key={r.w}>
                    <TD className="font-medium">{r.w}</TD>
                    <KpiCell>{fmtInt(r.st.r)}</KpiCell>
                    <KpiCell>{fmtInt(r.st.p)}</KpiCell>
                    <KpiCell><span className="text-run">{fmtInt(r.st.v30)}</span></KpiCell>
                    <KpiCell><span className="text-run">{fmtInt(r.st.vt)}</span></KpiCell>
                    <KpiCell><span className="text-info">{fmtInt(r.tm)}</span></KpiCell>
                    <PctCell ok={r.tm > 0 && r.verified === r.tm} value={r.tm ? pct(r.verified, r.tm) : null} title={r.tm ? `${r.verified} of ${r.tm} measure(s) verified` : undefined} />
                    <PctCell ok={r.tm > 0 && r.noDesc === 0} value={r.tm ? pct(r.tm - r.noDesc, r.tm) : null} title={r.noDesc ? `${r.noDesc} measure(s) without a description` : undefined} />
                    <KpiCell>
                      {r.unused > 0
                        ? <Badge variant="danger">{pct(r.unused, r.tm)}%</Badge>
                        : <Badge variant="success">0%</Badge>}
                    </KpiCell>
                    <KpiCell>
                      {ownersCount > 0
                        ? <span title={nameTooltip(r.owners)} className="cursor-help font-medium">{ownersCount}</span>
                        : <Badge variant="danger">0</Badge>}
                    </KpiCell>
                    <PctCell ok={r.tm > 0 && r.noOwner === 0} value={r.tm ? pct(r.tm - r.noOwner, r.tm) : null} title={r.noOwner ? `${r.noOwner} measure(s) without an owner` : undefined} />
                    <KpiCell>
                      {stewardsCount > 0
                        ? <span title={nameTooltip(r.stewards)} className="cursor-help font-medium">{stewardsCount}</span>
                        : <Badge variant="danger">0</Badge>}
                    </KpiCell>
                    <PctCell ok={r.tm > 0 && r.noSteward === 0} value={r.tm ? pct(r.tm - r.noSteward, r.tm) : null} title={r.noSteward ? `${r.noSteward} measure(s) without a steward` : undefined} />
                  </TR>
                );
              })
            )}
          </TBody>
        </Table>
      </Card>

      {/* Measure Pivot */}
      <Card className="overflow-hidden">
        <div className="border-b border-line p-5">
          <h2 className="text-[16px] font-semibold">Measure Pivot</h2>
          <div className="mt-4 flex flex-wrap items-end gap-3">
            <GovSelect
              label="Rows (Y axis)"
              value={pivotY}
              onChange={setPivotY}
              options={Object.keys(FIELD).map((k) => ({ value: k, label: cap(k) }))}
            />
            <GovSelect
              label="Columns (X axis)"
              value={pivotX}
              onChange={setPivotX}
              options={Object.keys(FIELD).map((k) => ({ value: k, label: cap(k) }))}
            />
            <GovSelect
              label="Measure"
              value={pivotMeasure}
              onChange={setPivotMeasure}
              options={[
                { value: "total", label: "Total Measures" },
                { value: "with_desc", label: "% with Description" },
              ]}
            />
            <span className="self-center text-[11px] text-faint">Pivot respects the filters above.</span>
          </div>
        </div>
        <div className="overflow-x-auto p-5">
          {pivot.rows.length === 0 ? (
            <p className="py-6 text-center italic text-faint">No measure data available.</p>
          ) : (
            <table className="w-full min-w-[560px] text-left text-[12px]">
              <thead className="text-faint">
                <tr className="border-b border-line">
                  <th className="px-2 py-2" />
                  {pivot.cols.map((c) => <th key={c} className="px-2 py-2 text-center font-semibold">{c}</th>)}
                  <th className="bg-panel2 px-2 py-2 text-center font-semibold">Total</th>
                </tr>
              </thead>
              <tbody>
                {pivot.rows.map((rk) => (
                  <tr key={rk} className="border-b border-line">
                    <td className="px-2 py-2 font-medium">{rk}</td>
                    {pivot.cols.map((ck) => {
                      const k = rk + " " + ck;
                      const t = pivot.tot[k] || 0;
                      const d = pivot.desc[k] || 0;
                      return (
                        <td key={ck} className="px-2 py-2 text-center">
                          {t ? <Badge variant="info">{pivot.asPct ? `${pct(d, t)}%` : t}</Badge> : <span className="text-faint">–</span>}
                        </td>
                      );
                    })}
                    <td className="bg-panel2 px-2 py-2 text-center font-semibold">
                      {pivot.rowTot[rk] ? (pivot.asPct ? `${pct(pivot.rowDesc[rk] || 0, pivot.rowTot[rk])}%` : pivot.rowTot[rk]) : "–"}
                    </td>
                  </tr>
                ))}
                <tr className="bg-panel2 font-semibold">
                  <td className="px-2 py-2">Total</td>
                  {pivot.cols.map((ck) => (
                    <td key={ck} className="px-2 py-2 text-center">
                      {pivot.colTot[ck] ? (pivot.asPct ? `${pct(pivot.colDesc[ck] || 0, pivot.colTot[ck])}%` : pivot.colTot[ck]) : "–"}
                    </td>
                  ))}
                  <td className="px-2 py-2 text-center">
                    {pivot.grandTot ? (pivot.asPct ? `${pct(pivot.grandDesc, pivot.grandTot)}%` : pivot.grandTot) : "–"}
                  </td>
                </tr>
              </tbody>
            </table>
          )}
        </div>
      </Card>
    </div>
  );
}

function cap(s: string) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function nameTooltip(counter: Record<string, number>): string {
  return Object.keys(counter)
    .sort((a, b) => counter[b] - counter[a] || a.localeCompare(b))
    .map((n) => `${n} (${counter[n]})`)
    .join("\n");
}

function GovSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: SelectOption[];
}) {
  return (
    <div>
      <label className="mb-1 block text-[11px] font-medium text-faint">{label}</label>
      <SimpleSelect value={value} onValueChange={onChange} options={options} className="min-w-[160px]" />
    </div>
  );
}

function KpiCell({ children }: { children: React.ReactNode }) {
  return <TD className="text-center">{children}</TD>;
}

function PctCell({ ok, value, title }: { ok: boolean; value: number | null; title?: string }) {
  if (value === null) return <TD className="text-center text-faint">—</TD>;
  return (
    <TD className="text-center">
      <span title={title} className={title ? "cursor-help" : undefined}>
        <Badge variant={ok ? "success" : "danger"}>{value}%</Badge>
      </span>
    </TD>
  );
}

