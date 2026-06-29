"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import { api, type UsageRow } from "@/lib/api";
import { fmtInt } from "@/lib/utils";

const DEFAULT_MONTH = "__default__";

interface Champion extends UsageRow {
  rank: number;
  reports_touched: number;
}

function initialsOf(name: string, email: string): string {
  const src = (name || "").trim() || (email || "").split("@")[0] || "?";
  const parts = src.split(/[\s._-]+/).filter(Boolean);
  const letters = parts.length >= 2 ? parts[0][0] + parts[parts.length - 1][0] : src.slice(0, 2);
  return letters.toUpperCase();
}

const SELECT_TRIGGER_CLS = "min-w-[150px]";

// Metallic medal palette shared by the podium cards and the leaderboard pills.
const RANK_GRADIENT: Record<number, string> = {
  1: "linear-gradient(135deg, #FFE066 0%, #E6B800 50%, #9A7B0E 100%)",
  2: "linear-gradient(135deg, #E0E0E0 0%, #A8A8A8 50%, #707070 100%)",
  3: "linear-gradient(135deg, #E5A872 0%, #CD7F32 50%, #7B4513 100%)",
};
const TROPHY: Record<number, string> = { 1: "🏆", 2: "🥈", 3: "🥉" };

function RankPill({ rank }: { rank: number }) {
  const grad = RANK_GRADIENT[rank];
  if (!grad) {
    return (
      <span className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-line bg-panel2 text-[12px] font-semibold text-muted-foreground">
        {rank}
      </span>
    );
  }
  return (
    <span
      className="inline-flex h-7 w-7 items-center justify-center rounded-full text-[12px] font-extrabold text-white"
      style={{ background: grad, textShadow: "0 1px 2px rgba(0,0,0,0.3)" }}
    >
      {rank}
    </span>
  );
}

function PodiumCard({ row, rank }: { row: Champion | null; rank: number }) {
  const grad = RANK_GRADIENT[rank];
  const isChamp = rank === 1;
  const name = row ? row.user_display_name || (row.user_email || "").split("@")[0] || "Unknown" : "";
  const email = row?.user_email || "";
  return (
    <div
      className={`relative flex flex-col items-center overflow-hidden rounded-2xl p-5 text-center ${
        isChamp ? "border-2 border-[#fcd34d] bg-[linear-gradient(135deg,#fefce8_0%,#fef3c7_100%)] shadow-lg" : "border border-line bg-panel2 shadow-card"
      } ${row ? "" : "opacity-60"}`}
      style={{ minHeight: isChamp ? "17rem" : "14rem" }}
    >
      <span
        className="pointer-events-none absolute right-3 top-1 select-none font-black leading-none"
        style={{ fontSize: "7rem", color: isChamp ? "rgba(230,184,0,0.18)" : "rgba(15,23,42,0.05)" }}
      >
        {rank}
      </span>
      <div className="relative flex flex-col items-center">
        <div style={{ fontSize: isChamp ? "2.25rem" : "1.75rem" }}>{TROPHY[rank]}</div>
        <div
          className="mt-2 flex items-center justify-center rounded-full font-extrabold text-white"
          style={{
            width: isChamp ? "5rem" : "3.5rem",
            height: isChamp ? "5rem" : "3.5rem",
            fontSize: isChamp ? "1.4rem" : "1rem",
            background: grad,
            boxShadow: "0 0 0 4px var(--card), 0 6px 18px rgba(0,0,0,0.12)",
            textShadow: "0 1px 2px rgba(0,0,0,0.3)",
          }}
        >
          {row ? initialsOf(name, email) : "—"}
        </div>
        {row ? (
          <>
            <div className={`mt-3 truncate font-bold ${isChamp ? "text-[15px]" : "text-[13px]"}`} title={name}>
              {name}
            </div>
            <div className="truncate text-[12px] text-muted-foreground" title={email}>
              {email}
            </div>
            <div
              className={`mt-3 font-extrabold tabular-nums ${isChamp ? "text-3xl" : "text-2xl"}`}
              style={{ letterSpacing: "-0.025em" }}
            >
              {fmtInt(row.view_count)}
            </div>
            <div className="mt-0.5 text-[10px] font-semibold uppercase tracking-[0.15em] text-faint">
              views{row.reports_touched != null ? ` · ${fmtInt(row.reports_touched)} reports` : ""}
            </div>
          </>
        ) : (
          <div className="mt-3 text-[13px] font-semibold text-faint">No champion</div>
        )}
      </div>
    </div>
  );
}

export default function ChampionsPage() {
  const [ws, setWs] = useState("");
  const [monthState, setMonthState] = useState(DEFAULT_MONTH);

  const filtersQ = useQuery({ queryKey: ["filters"], queryFn: () => api.filters(), staleTime: 5 * 60_000 });
  const monthsQ = useQuery({
    queryKey: ["usage-months"],
    queryFn: () => api.powerbiUsage({ limit: 1 }),
    staleTime: 5 * 60_000,
  });

  const months = monthsQ.data?.months ?? [];
  const month = monthState === DEFAULT_MONTH ? months[0] ?? "" : monthState;

  const boardQ = useQuery({
    queryKey: ["champions", ws, month],
    enabled: monthsQ.isSuccess,
    queryFn: async (): Promise<Champion[]> => {
      const r1 = await api.powerbiUsage({
        group_by: "user_email,user_display_name",
        workspace_name: ws || undefined,
        month: month || undefined,
        limit: 500,
      });
      const rows = r1.results.filter((r) => r.user_email);
      if (!rows.length) return [];
      const r2 = await api.powerbiUsage({
        group_by: "user_email,report_id",
        workspace_name: ws || undefined,
        month: month || undefined,
        limit: 5000,
      });
      const touched: Record<string, Set<string>> = {};
      for (const r of r2.results) {
        const key = r.user_email || "";
        if (!key) continue;
        (touched[key] = touched[key] || new Set()).add(r.report_id || "");
      }
      return rows.map((r, i) => ({
        ...r,
        rank: i + 1,
        reports_touched: (touched[r.user_email || ""] || new Set()).size,
      }));
    },
  });

  const rows = boardQ.data ?? [];
  const podium = [rows[1] ?? null, rows[0] ?? null, rows[2] ?? null]; // 2 — 1 — 3
  const podiumRanks = [2, 1, 3];
  const loading = monthsQ.isLoading || boardQ.isLoading || boardQ.isFetching;

  return (
    <div>
      <PageHeader
        title="Data Champions"
        description="The people behind the views — ranked by total report views in the selected period."
      />

      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-end gap-4 border-b border-line bg-panel2/40 p-4">
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
        </div>

        {loading && <LoadingState label="Crowning the champions…" />}
        {!loading && boardQ.isError && (
          <EmptyState title="Failed to load champions" hint="Try refreshing." />
        )}
        {!loading && !boardQ.isError && rows.length === 0 && (
          <EmptyState title="No usage in this period yet." hint="Pick a different workspace or month." />
        )}

        {!loading && rows.length > 0 && (
          <div className="p-6">
            <div className="mx-auto grid max-w-3xl grid-cols-1 items-end gap-5 md:grid-cols-3">
              {podium.map((row, i) => (
                <PodiumCard key={podiumRanks[i]} row={row} rank={podiumRanks[i]} />
              ))}
            </div>

            <div className="mt-8 overflow-hidden rounded-lg border border-line">
              <div className="border-b border-line bg-panel2/50 px-4 py-3">
                <h3 className="text-[13px] font-semibold">Full Leaderboard</h3>
                <p className="mt-0.5 text-[12px] text-muted-foreground">
                  All ranked viewers for the selected period.
                </p>
              </div>
              <Table>
                <THead>
                  <TR>
                    <TH className="w-[60px] text-center">Rank</TH>
                    <TH>Champion</TH>
                    <TH>Email</TH>
                    <TH className="text-right">Total Views</TH>
                    <TH className="text-right">Reports Touched</TH>
                  </TR>
                </THead>
                <TBody>
                  {rows.map((r) => {
                    const name = r.user_display_name || (r.user_email || "").split("@")[0] || "Unknown";
                    return (
                      <TR key={r.user_email}>
                        <TD className="text-center"><RankPill rank={r.rank} /></TD>
                        <TD>
                          <div className="flex items-center gap-2">
                            <span className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-line bg-panel2 text-[11px] font-bold text-muted-foreground">
                              {initialsOf(name, r.user_email || "")}
                            </span>
                            <span className="font-medium">{name}</span>
                          </div>
                        </TD>
                        <TD className="text-muted-foreground">{r.user_email || "—"}</TD>
                        <TD className="text-right font-semibold">{fmtInt(r.view_count)}</TD>
                        <TD className="text-right text-muted-foreground">{fmtInt(r.reports_touched)}</TD>
                      </TR>
                    );
                  })}
                </TBody>
              </Table>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
