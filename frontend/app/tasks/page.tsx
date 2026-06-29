"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Search } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import { api, unwrapResults, type DataPerson, type GovernanceTask } from "@/lib/api";

const TASKS_QK = ["tasks-all"];

function fmtDateTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusBadge(s: string | null) {
  if (s === "ATTENTION") return <Badge variant="warning">Attention</Badge>;
  if (s === "DELETED") return <Badge variant="danger">Deletion</Badge>;
  return <Badge>{s || "—"}</Badge>;
}

function Assignee({ t }: { t: GovernanceTask }) {
  if (!t.assignee_name) return <span className="italic text-faint">Unassigned</span>;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span className="font-medium">{t.assignee_name}</span>
      {t.assignee_role && (
        <span
          className="rounded border border-line-strong bg-panel2 px-1.5 py-px text-[10px] font-bold uppercase text-faint"
          title={`Routed from the asset's ${t.assignee_role}`}
        >
          {t.assignee_role}
        </span>
      )}
      {t.assignee_slack && <span className="text-[11px] text-faint">{t.assignee_slack}</span>}
    </span>
  );
}

export default function TasksPage() {
  const qc = useQueryClient();
  const [assignee, setAssignee] = useState("");
  const [trigger, setTrigger] = useState("");
  const [q, setQ] = useState("");

  const tasksQ = useQuery({
    queryKey: TASKS_QK,
    queryFn: () => api.tasks.list({ state: "all", limit: 100000, ordering: "-created_at" }),
  });
  const stewardsQ = useQuery({
    queryKey: ["task-stewards"],
    queryFn: () => api.dataPersons.list({ is_steward: true }),
    staleTime: 5 * 60_000,
  });

  const doneMut = useMutation({
    mutationFn: (id: number) => api.tasks.done(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: TASKS_QK }),
  });

  const stewards = unwrapResults<DataPerson>(stewardsQ.data);
  const all = tasksQ.data?.results ?? [];

  const matches = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (t: GovernanceTask) => {
      if (assignee === "none" && t.assignee) return false;
      if (assignee && assignee !== "none" && String(t.assignee ?? "") !== assignee) return false;
      if (trigger && (t.trigger_status || "") !== trigger) return false;
      if (needle) {
        const hay = `${t.title || ""} ${t.item_name || ""} ${t.asset_context || ""}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    };
  }, [assignee, trigger, q]);

  const open = all.filter((t) => t.state === "open").filter(matches);
  const done = all.filter((t) => t.state === "done").filter(matches);

  return (
    <div>
      <PageHeader
        title="Task Manager"
        description="Follow-up tasks created automatically when an asset is set to Attention or Deletion. Tasks go to the asset's steward; mark one Done once handled."
      />

      {/* shared filters */}
      <Card className="mb-4">
        <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label className="mb-1 block text-[11px] font-medium text-faint">Assignee</label>
            <SimpleSelect
              value={assignee}
              onValueChange={setAssignee}
              options={[
                { value: "", label: "All assignees" },
                { value: "none", label: "Unassigned" },
                ...stewards.map((p) => ({ value: String(p.id), label: p.name })),
              ]}
            />
          </div>
          <div>
            <label className="mb-1 block text-[11px] font-medium text-faint">Status</label>
            <SimpleSelect
              value={trigger}
              onValueChange={setTrigger}
              options={[
                { value: "", label: "All" },
                { value: "ATTENTION", label: "Attention" },
                { value: "DELETED", label: "Deletion" },
              ]}
            />
          </div>
          <div className="lg:col-span-2">
            <label className="mb-1 block text-[11px] font-medium text-faint">Search</label>
            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-panel px-3 text-[13px]">
              <Search className="h-3.5 w-3.5 text-faint" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Filter by task or asset…"
                className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-faint"
              />
            </div>
          </div>
        </div>
      </Card>

      {tasksQ.isLoading && (
        <Card>
          <LoadingState label="Loading tasks…" />
        </Card>
      )}
      {tasksQ.isError && (
        <Card>
          <EmptyState title="Failed to load tasks" hint="The tasks API returned an error." />
        </Card>
      )}

      {!tasksQ.isLoading && !tasksQ.isError && (
        <div className="space-y-6">
          {/* Open */}
          <Card className="overflow-hidden">
            <div className="flex items-center gap-2 border-b border-line bg-panel2/50 px-4 py-3">
              <h3 className="text-[13px] font-bold uppercase tracking-wide">Open Tasks</h3>
              <Badge variant="brand">{open.length}</Badge>
            </div>
            {open.length === 0 ? (
              <EmptyState title="No open tasks 🎉" hint="New tasks appear when an asset is flagged." />
            ) : (
              <Table>
                <THead>
                  <TR>
                    <TH className="min-w-[260px]">Task</TH>
                    <TH className="min-w-[180px]">Asset</TH>
                    <TH>Status</TH>
                    <TH className="min-w-[150px]">Assignee</TH>
                    <TH>Created</TH>
                    <TH className="text-right">Action</TH>
                  </TR>
                </THead>
                <TBody>
                  {open.map((t) => (
                    <TR key={t.id}>
                      <TD>
                        <div className="font-semibold">{t.title || "—"}</div>
                        {t.asset_context && (
                          <div className="mt-0.5 text-[11px] text-faint">{t.asset_context}</div>
                        )}
                      </TD>
                      <TD>
                        {t.web_url ? (
                          <a href={t.web_url} target="_blank" rel="noreferrer" className="hover:underline">
                            {t.item_name || "—"}
                          </a>
                        ) : (
                          t.item_name || "—"
                        )}
                      </TD>
                      <TD>{statusBadge(t.trigger_status)}</TD>
                      <TD><Assignee t={t} /></TD>
                      <TD className="whitespace-nowrap text-[12px]">{fmtDateTime(t.created_at)}</TD>
                      <TD className="text-right">
                        <Button
                          variant="brand"
                          size="sm"
                          disabled={doneMut.isPending}
                          onClick={() => doneMut.mutate(t.id)}
                        >
                          <Check /> Done
                        </Button>
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )}
          </Card>

          {/* Completed */}
          <Card className="overflow-hidden opacity-90">
            <div className="flex items-center gap-2 border-b border-line bg-panel2/50 px-4 py-3">
              <h3 className="text-[13px] font-bold uppercase tracking-wide text-muted-foreground">Completed</h3>
              <Badge>{done.length}</Badge>
              <span className="text-[11px] text-faint">— kept for tracking</span>
            </div>
            {done.length === 0 ? (
              <EmptyState title="No completed tasks yet." />
            ) : (
              <Table>
                <THead>
                  <TR>
                    <TH className="min-w-[260px]">Task</TH>
                    <TH className="min-w-[180px]">Asset</TH>
                    <TH>Status</TH>
                    <TH className="min-w-[150px]">Assignee</TH>
                    <TH>Created</TH>
                    <TH>Completed</TH>
                  </TR>
                </THead>
                <TBody>
                  {done.map((t) => (
                    <TR key={t.id} className="text-muted-foreground">
                      <TD>
                        <div className="font-semibold">{t.title || "—"}</div>
                        {t.asset_context && (
                          <div className="mt-0.5 text-[11px] text-faint">{t.asset_context}</div>
                        )}
                      </TD>
                      <TD>
                        {t.web_url ? (
                          <a href={t.web_url} target="_blank" rel="noreferrer" className="hover:underline">
                            {t.item_name || "—"}
                          </a>
                        ) : (
                          t.item_name || "—"
                        )}
                      </TD>
                      <TD>{statusBadge(t.trigger_status)}</TD>
                      <TD><Assignee t={t} /></TD>
                      <TD className="whitespace-nowrap text-[12px]">{fmtDateTime(t.created_at)}</TD>
                      <TD>
                        <Badge variant="outline">Done {fmtDateTime(t.completed_at)}</Badge>
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
