"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ListChecks, Search, Sparkles, TriangleAlert } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAuth } from "@/lib/auth";
import {
  api,
  getApiErrorMessage,
  unwrapResults,
  type DataPerson,
  type GovernanceTask,
  type TaskSweepResult,
} from "@/lib/api";

/** Which DataPerson the viewer is acting as. Most logins aren't linked to a
 *  DataPerson yet, so the person picks their own name and we remember it. */
const PERSON_KEY = "datagov.tasks.actingPersonId";

/** Rows per page. Production has ~4,100 open tasks, so the board has to page —
 *  a single fat request silently dropped everything past the limit. */
const PAGE_SIZE = 200;

type Scope = "mine" | "unassigned" | "all" | "done";

/** Delay a fast-changing value. The search box feeds the react-query key, so
 *  without this every keystroke fired a full page request. */
function useDebounced<T>(value: T, ms = 350): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

/** The reason groups, in the order the work should be tackled. Hints are the
 *  customer's own framing of why each task exists — the assignee needs to know
 *  the intent, not just the title. */
const REASONS: { key: string; label: string; hint: string }[] = [
  { key: "UNVERIFIED", label: "Verify", hint: "You own these — move each to Verified, or flag it for Attention." },
  { key: "NO_CATEGORY", label: "Category", hint: "You own these — give each measure a category." },
  { key: "ATTENTION", label: "Attention", hint: "You steward these — agree with the Owner whether they get deleted." },
  { key: "DELETED", label: "To Be Deleted", hint: "You steward these — confirm the asset can go, or restore it." },
];

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

/** Age badge: no due dates exist, so age is the "this is rotting" signal. */
function AgeBadge({ days }: { days: number | null }) {
  if (days === null || days === undefined) return <span className="text-faint">—</span>;
  const variant = days >= 30 ? "danger" : days >= 14 ? "warning" : "outline";
  return <Badge variant={variant}>{days}d</Badge>;
}

function reasonBadge(reason: string, label: string) {
  const variant =
    reason === "ATTENTION" ? "warning" : reason === "DELETED" ? "danger" : reason === "UNVERIFIED" ? "info" : "brand";
  return <Badge variant={variant}>{label}</Badge>;
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
    </span>
  );
}

export default function TasksPage() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const isAdmin = !!(user?.perms?.is_admin || user?.role === "admin");

  const [scope, setScope] = useState<Scope>("mine");
  const [reason, setReason] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [genOpen, setGenOpen] = useState(false);
  const [page, setPage] = useState(1);

  // Acting identity: the linked DataPerson if the login has one, else whatever
  // name the person picked (remembered per browser until the accounts are
  // properly linked).
  const [personId, setPersonId] = useState<string>("");
  useEffect(() => {
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(PERSON_KEY) : null;
    if (stored) setPersonId(stored);
    else if (user?.data_person?.id) setPersonId(String(user.data_person.id));
  }, [user?.data_person?.id]);

  function pickPerson(value: string) {
    setPersonId(value);
    if (typeof window !== "undefined") {
      if (value) window.localStorage.setItem(PERSON_KEY, value);
      else window.localStorage.removeItem(PERSON_KEY);
    }
  }

  const peopleQ = useQuery({
    queryKey: ["task-people"],
    // Owners AND stewards: both roles receive tasks now, so a steward-only list
    // would hide most assignees.
    queryFn: async () => {
      const [owners, stewards] = await Promise.all([
        api.dataPersons.list({ is_owner: true }),
        api.dataPersons.list({ is_steward: true }),
      ]);
      const byId = new Map<number, DataPerson>();
      for (const p of [...unwrapResults<DataPerson>(owners), ...unwrapResults<DataPerson>(stewards)]) {
        byId.set(p.id, p);
      }
      return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
    },
    staleTime: 5 * 60_000,
  });
  const people = peopleQ.data ?? [];

  const search = useDebounced(q);
  const listParams = useMemo(() => {
    const p: Record<string, string | number | undefined> = {
      limit: PAGE_SIZE,
      page,
      ordering: "-created_at",
      state: scope === "done" ? "done" : "open",
    };
    if (scope === "mine") {
      // An explicitly picked name wins; `scope=mine` is the fallback for logins
      // that ARE linked to a DataPerson.
      if (personId) p.assignee = personId;
      else p.scope = "mine";
    } else if (scope === "unassigned") {
      p.scope = "unassigned";
    }
    if (reason) p.reason = reason;
    if (search.trim()) p.search = search.trim();
    return p;
  }, [scope, personId, reason, search, page]);

  const tasksQ = useQuery({
    queryKey: ["tasks", listParams],
    queryFn: () => api.tasks.list(listParams),
  });
  const summaryQ = useQuery({
    queryKey: ["tasks-summary", personId],
    queryFn: () => api.tasks.summary(personId ? { person: personId } : {}),
  });

  const rows = tasksQ.data?.results ?? [];
  const summary = summaryQ.data;

  // Any change to what's listed drops the selection and returns to page 1.
  // Otherwise ticking rows, then narrowing the filter, then pressing "Mark N
  // done" would close tasks that are no longer on screen — the one genuinely
  // destructive thing this page can do.
  useEffect(() => {
    setSelected(new Set());
    setPage(1);
  }, [reason, search, personId, scope]);

  const total = tasksQ.data?.count ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const firstRow = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const lastRow = Math.min(page * PAGE_SIZE, total);

  function refresh() {
    qc.invalidateQueries({ queryKey: ["tasks"] });
    qc.invalidateQueries({ queryKey: ["tasks-summary"] });
    setSelected(new Set());
  }

  const [bulkNote, setBulkNote] = useState<string | null>(null);

  const doneMut = useMutation({
    mutationFn: (id: number) => api.tasks.done(id),
    onSuccess: refresh,
  });
  const bulkMut = useMutation({
    mutationFn: (ids: number[]) => api.tasks.bulkDone(ids),
    onSuccess: (res) => {
      // Report what actually closed. `updated` can be lower than `requested`
      // when a task was already done or belongs to another org — saying so
      // beats letting the number quietly disagree with the button.
      setBulkNote(
        res.updated === res.requested
          ? `Closed ${res.updated} task${res.updated === 1 ? "" : "s"}.`
          : `Closed ${res.updated} of ${res.requested} — the rest were already done.`,
      );
      refresh();
    },
    onError: (e) => setBulkNote(getApiErrorMessage(e, "Could not close the selected tasks.")),
  });

  // Selection is scoped to what's currently on screen, so "select all" can never
  // silently close something the user can't see. The header checkbox lives in a
  // reason group's table, so it selects THAT group — a single checkbox that
  // silently swept up rows in other tables would be a nasty surprise on a
  // destructive-ish action.
  function toggleGroup(ids: number[], selectAll: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        if (selectAll) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }
  function toggleOne(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const grouped = useMemo(() => {
    const byReason = new Map<string, GovernanceTask[]>();
    for (const t of rows) {
      const key = String(t.reason || "OTHER");
      if (!byReason.has(key)) byReason.set(key, []);
      byReason.get(key)!.push(t);
    }
    const ordered = REASONS.filter((r) => byReason.has(r.key)).map((r) => ({
      ...r,
      tasks: byReason.get(r.key)!,
    }));
    for (const [key, tasks] of byReason) {
      if (!REASONS.some((r) => r.key === key)) {
        ordered.push({ key, label: tasks[0]?.reason_label || key, hint: "", tasks });
      }
    }
    return ordered;
  }, [rows]);

  const showPersonPrompt = scope === "mine" && !personId && !summary?.linked;

  return (
    <div>
      <PageHeader
        title="Task Manager"
        description="Governance follow-ups: unverified and uncategorised measures go to their Owner, flagged assets to their Steward. Mark one Done once handled."
        actions={
          isAdmin ? (
            <Button variant="brand" size="sm" onClick={() => setGenOpen(true)}>
              <Sparkles /> Generate tasks
            </Button>
          ) : undefined
        }
      />

      {/* Who am I — until logins are linked to data people, the viewer says so. */}
      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3 p-4">
          <div className="min-w-[220px]">
            <label className="mb-1 block text-[11px] font-medium text-faint">I am</label>
            <SimpleSelect
              value={personId}
              onValueChange={pickPerson}
              options={[
                { value: "", label: summary?.linked ? "My linked profile" : "— pick your name —" },
                ...people.map((p) => ({ value: String(p.id), label: p.name })),
              ]}
            />
          </div>
          <div className="min-w-[150px]">
            <label className="mb-1 block text-[11px] font-medium text-faint">Reason</label>
            <SimpleSelect
              value={reason}
              onValueChange={setReason}
              options={[
                { value: "", label: "All reasons" },
                ...REASONS.map((r) => ({ value: r.key, label: r.label })),
              ]}
            />
          </div>
          <div className="min-w-[220px] flex-1">
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

      <Tabs value={scope} onValueChange={(v) => setScope(v as Scope)}>
        <TabsList className="mb-4">
          <TabsTrigger value="mine">
            Mine {summary ? <span className="ml-1.5 text-faint">{summary.mine_open}</span> : null}
          </TabsTrigger>
          <TabsTrigger value="unassigned">
            Unassigned {summary ? <span className="ml-1.5 text-faint">{summary.unassigned_open}</span> : null}
          </TabsTrigger>
          <TabsTrigger value="all">
            Everyone {summary ? <span className="ml-1.5 text-faint">{summary.total_open}</span> : null}
          </TabsTrigger>
          <TabsTrigger value="done">
            Completed {summary ? <span className="ml-1.5 text-faint">{summary.done_total}</span> : null}
          </TabsTrigger>
        </TabsList>
      </Tabs>

      {showPersonPrompt && (
        <Card className="mb-4 border-warn/40">
          <div className="flex items-start gap-2.5 p-4 text-[13px]">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
            <div>
              <div className="font-semibold">Pick your name to see your tasks</div>
              <div className="mt-0.5 text-muted-foreground">
                Your login isn&apos;t linked to a data person yet, so we can&apos;t tell which tasks are
                yours. Choose your name in <span className="font-medium">I am</span> above — we&apos;ll remember it.
              </div>
            </div>
          </div>
        </Card>
      )}

      {selected.size > 0 && (
        <div className="mb-3 flex items-center gap-3 rounded-lg border border-brand/40 bg-brand/5 px-4 py-2.5">
          <ListChecks className="h-4 w-4 text-brand" />
          <span className="text-[13px] font-medium">{selected.size} selected</span>
          <Button
            variant="brand"
            size="sm"
            disabled={bulkMut.isPending}
            onClick={() => bulkMut.mutate([...selected])}
          >
            <Check /> Mark {selected.size} done
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
          <span className="text-[11px] text-faint">
            selects rows on this page only
          </span>
        </div>
      )}

      {bulkNote && (
        <div className="mb-3 flex items-center justify-between gap-3 rounded-lg border border-line bg-panel2/50 px-4 py-2 text-[12px]">
          <span>{bulkNote}</span>
          <button className="text-faint hover:text-foreground" onClick={() => setBulkNote(null)}>
            Dismiss
          </button>
        </div>
      )}

      {tasksQ.isLoading && (
        <Card>
          <LoadingState label="Loading tasks…" />
        </Card>
      )}
      {tasksQ.isError && (
        <Card>
          <EmptyState
            title="Failed to load tasks"
            hint={getApiErrorMessage(tasksQ.error, "The tasks API returned an error.")}
          />
        </Card>
      )}

      {!tasksQ.isLoading && !tasksQ.isError && rows.length === 0 && (
        <Card>
          <EmptyState
            title={scope === "done" ? "Nothing completed yet." : "No open tasks 🎉"}
            hint={
              scope === "mine"
                ? "Nothing is assigned to you right now."
                : "New tasks appear when an asset is flagged, or when an admin runs Generate."
            }
          />
        </Card>
      )}

      {!tasksQ.isLoading && !tasksQ.isError && rows.length > 0 && (
        <div className="space-y-5">
          {grouped.map((group) => {
            const groupIds = group.tasks.map((t) => t.id);
            const groupAllSelected = groupIds.every((id) => selected.has(id));
            return (
            <Card key={group.key} className="overflow-hidden">
              <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel2/50 px-4 py-3">
                <h3 className="text-[13px] font-bold uppercase tracking-wide">{group.label}</h3>
                <Badge variant="brand">{group.tasks.length}</Badge>
                {group.hint && <span className="text-[11px] text-faint">— {group.hint}</span>}
              </div>
              <Table>
                <THead>
                  <TR>
                    {scope !== "done" && (
                      <TH className="w-[40px]">
                        <input
                          type="checkbox"
                          aria-label={`Select all ${group.label} tasks`}
                          checked={groupAllSelected}
                          onChange={() => toggleGroup(groupIds, !groupAllSelected)}
                          className="h-3.5 w-3.5 accent-[oklch(var(--welcome-teal))]"
                        />
                      </TH>
                    )}
                    <TH className="min-w-[260px]">Task</TH>
                    <TH className="min-w-[180px]">Asset</TH>
                    <TH>Reason</TH>
                    <TH className="min-w-[150px]">Assignee</TH>
                    <TH>Age</TH>
                    <TH>{scope === "done" ? "Completed" : "Created"}</TH>
                    {scope !== "done" && <TH className="text-right">Action</TH>}
                  </TR>
                </THead>
                <TBody>
                  {group.tasks.map((t) => (
                    <TR key={t.id}>
                      {scope !== "done" && (
                        <TD>
                          <input
                            type="checkbox"
                            aria-label={`Select task ${t.title}`}
                            checked={selected.has(t.id)}
                            onChange={() => toggleOne(t.id)}
                            className="h-3.5 w-3.5 accent-[oklch(var(--welcome-teal))]"
                          />
                        </TD>
                      )}
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
                      <TD>{reasonBadge(String(t.reason), t.reason_label || String(t.reason))}</TD>
                      <TD>
                        <Assignee t={t} />
                      </TD>
                      <TD>
                        <AgeBadge days={t.age_days} />
                      </TD>
                      <TD className="whitespace-nowrap text-[12px]">
                        {scope === "done" ? (
                          <span className="inline-flex items-center gap-1.5">
                            {fmtDateTime(t.completed_at)}
                            {t.closed_reason === "resolved" && (
                              <Badge variant="outline" title="Closed automatically because the gap was fixed">
                                auto
                              </Badge>
                            )}
                          </span>
                        ) : (
                          fmtDateTime(t.created_at)
                        )}
                      </TD>
                      {scope !== "done" && (
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
                      )}
                    </TR>
                  ))}
                </TBody>
              </Table>
            </Card>
            );
          })}

          {/* Paging. The board is grouped by reason WITHIN a page, so the group
              badges count the page — the line below is the honest total. */}
          <div className="flex flex-wrap items-center justify-between gap-3 px-1 text-[12px] text-muted-foreground">
            <span>
              Showing <b>{firstRow}</b>–<b>{lastRow}</b> of <b>{total}</b>
              {totalPages > 1 && <> · page {page} of {totalPages}</>}
            </span>
            {totalPages > 1 && (
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1 || tasksQ.isFetching}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages || tasksQ.isFetching}
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                >
                  Next
                </Button>
              </div>
            )}
          </div>
        </div>
      )}

      <GenerateDialog open={genOpen} onOpenChange={setGenOpen} onDone={refresh} />
    </div>
  );
}

/** Admin-only sweep. Preview first — it runs the same reconciliation with
 *  dry_run and writes nothing, so nobody commits to a few thousand rows blind. */
function GenerateDialog({
  open,
  onOpenChange,
  onDone,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onDone: () => void;
}) {
  const [picked, setPicked] = useState<string[]>(REASONS.map((r) => r.key));
  const [kindScope, setKindScope] = useState("measure_name");
  const [preview, setPreview] = useState<TaskSweepResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The reasons and scopes are backend policy — read them rather than
  // hard-coding a second copy that can drift.
  const optionsQ = useQuery({
    queryKey: ["task-generate-options"],
    queryFn: () => api.tasks.generateOptions(),
    enabled: open,
    staleTime: 60 * 60_000,
  });
  const scopes = optionsQ.data?.kind_scopes ?? [
    { key: "measure_name", label: "PowerBI measures", hint: "" },
  ];

  const previewMut = useMutation({
    mutationFn: () => api.tasks.generate({ reasons: picked, dry_run: true, kind_scope: kindScope }),
    onSuccess: (r) => {
      setPreview(r);
      setError(null);
    },
    onError: (e) => setError(getApiErrorMessage(e, "Preview failed.")),
  });
  const runMut = useMutation({
    mutationFn: () => api.tasks.generate({ reasons: picked, dry_run: false, kind_scope: kindScope }),
    onSuccess: (r) => {
      setPreview(r);
      setError(null);
      onDone();
    },
    onError: (e) => setError(getApiErrorMessage(e, "Generate failed.")),
  });

  function toggle(key: string) {
    setPreview(null);
    setPicked((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Generate governance tasks</DialogTitle>
          <DialogDescription>
            Creates the missing tasks, re-resolves assignees on tasks already open, and closes the ones
            whose gap has since been fixed. Safe to run repeatedly.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          {REASONS.map((r) => (
            <label key={r.key} className="flex cursor-pointer items-start gap-2.5 text-[13px]">
              <input
                type="checkbox"
                checked={picked.includes(r.key)}
                onChange={() => toggle(r.key)}
                className="mt-0.5 h-3.5 w-3.5 accent-[oklch(var(--welcome-teal))]"
              />
              <span>
                <span className="font-medium">{r.label}</span>
                <span className="block text-[11px] text-faint">{r.hint}</span>
              </span>
            </label>
          ))}
        </div>

        <div>
          <label className="mb-1 block text-[11px] font-medium text-faint">Applies to</label>
          <SimpleSelect
            value={kindScope}
            onValueChange={(v) => {
              setPreview(null);
              setKindScope(v);
            }}
            options={scopes.map((s) => ({ value: s.key, label: s.label }))}
          />
          <p className="mt-1 text-[11px] text-faint">
            {scopes.find((s) => s.key === kindScope)?.hint}
          </p>
          {kindScope !== "measure_name" && (
            <p className="mt-1 text-[11px] font-medium text-warn">
              Preview this first — anything wider than measures runs into tens of thousands of tasks.
            </p>
          )}
        </div>

        {error && (
          <div className="rounded-md border border-err/40 bg-err/5 px-3 py-2 text-[12px] text-err">{error}</div>
        )}

        {preview && (
          <div className="rounded-md border border-line bg-panel2/50 px-3 py-2.5 text-[12px]">
            <div className="mb-1 font-semibold">
              {preview.dry_run ? "Preview — nothing was written" : "Done"}
            </div>
            <div className="text-muted-foreground">
              {preview.dry_run ? "Would create" : "Created"} <b>{preview.totals.created}</b> · close{" "}
              <b>{preview.totals.closed}</b> · reassign <b>{preview.totals.reassigned}</b>
              {preview.totals.unassigned > 0 && (
                <>
                  {" "}
                  · <span className="text-warn">{preview.totals.unassigned} with no Owner or Steward</span>
                </>
              )}
            </div>
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={previewMut.isPending || picked.length === 0}
            onClick={() => previewMut.mutate()}
          >
            Preview
          </Button>
          <Button
            variant="brand"
            size="sm"
            disabled={runMut.isPending || picked.length === 0}
            onClick={() => runMut.mutate()}
          >
            <Sparkles /> Generate
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
