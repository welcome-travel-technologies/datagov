"use client";

import { useEffect, useMemo, useRef, useState } from "react";
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
  buildGenerationCommitRequest,
  buildTaskListParams,
  generationPreviewAuthorizes,
  generationPreviewKey,
  generationPreviewTokenRequired,
  TASK_DEFAULT_KIND_SCOPE,
  TASK_PAGE_SIZE as PAGE_SIZE,
  TASK_REASON_UI as REASONS,
  visibleSelectedTaskIds,
  type TaskScope,
} from "@/app/tasks/task-manager-state";
import {
  api,
  getApiErrorMessage,
  unwrapResults,
  type DataPerson,
  type GovernanceTask,
  type TaskSweepResult,
} from "@/lib/api";

const EMPTY_TASKS: GovernanceTask[] = [];

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

  const [scope, setScope] = useState<TaskScope>("mine");
  const [reason, setReason] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [genOpen, setGenOpen] = useState(false);
  const [page, setPage] = useState(1);

  // Only admins may inspect another person's Mine view. Ordinary members are
  // always derived from the login-linked DataPerson on the server.
  const [personId, setPersonId] = useState<string>("");
  useEffect(() => {
    if (!isAdmin) {
      setPersonId("");
      setScope("mine");
    }
  }, [isAdmin]);

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
    enabled: isAdmin,
  });
  const people = peopleQ.data ?? [];

  const search = useDebounced(q);
  const listParams = useMemo(
    () => buildTaskListParams({ scope, isAdmin, personId, reason, search, page }),
    [scope, personId, reason, search, page, isAdmin],
  );

  const tasksQ = useQuery({
    queryKey: ["tasks", listParams],
    queryFn: () => api.tasks.list(listParams),
  });
  const summaryQ = useQuery({
    queryKey: ["tasks-summary", isAdmin ? personId : "linked"],
    queryFn: () => api.tasks.summary(isAdmin && personId ? { person: personId } : {}),
  });

  const rows = tasksQ.data?.results ?? EMPTY_TASKS;
  const summary = summaryQ.data;
  const visibleSelectedIds = useMemo(
    () => visibleSelectedTaskIds(rows.map((row) => row.id), selected),
    [rows, selected],
  );

  // A filter change drops the selection and returns to page 1.
  // Otherwise ticking rows, then narrowing the filter, then pressing "Mark N
  // done" would close tasks that are no longer on screen — the one genuinely
  // destructive thing this page can do.
  useEffect(() => {
    setSelected(new Set());
    setPage(1);
  }, [reason, search, personId, scope]);

  // A page change also drops selection, but must not jump back to page 1.
  // This keeps Bulk Done limited to rows that are still visible.
  useEffect(() => {
    setSelected(new Set());
  }, [page]);

  const total = tasksQ.data?.count ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const firstRow = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const lastRow = Math.min(page * PAGE_SIZE, total);

  function refresh() {
    setPage(1);
    qc.invalidateQueries({ queryKey: ["tasks"] });
    qc.invalidateQueries({ queryKey: ["tasks-summary"] });
    setSelected(new Set());
  }

  const [bulkNote, setBulkNote] = useState<string | null>(null);

  const doneMut = useMutation({
    mutationFn: (id: number) => api.tasks.done(id),
    onSuccess: refresh,
    onError: (e) => setBulkNote(getApiErrorMessage(e, "Could not close this task.")),
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
    const ordered: { key: string; label: string; hint: string; tasks: GovernanceTask[] }[] =
      REASONS.filter((r) => byReason.has(r.key)).map((r) => ({
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

  const showPersonPrompt =
    scope === "mine" &&
    !(isAdmin && personId) &&
    !!summary &&
    (summary.identity_required === true || !summary.linked);

  return (
    <div>
      <PageHeader
        title="Task Manager"
        description="Governance follow-ups: unverified measures and uncategorised assets go to their Owner, flagged assets to their Steward. Mark one Done once handled."
        actions={
          isAdmin ? (
            <Button variant="brand" size="sm" onClick={() => setGenOpen(true)}>
              <Sparkles /> Generate tasks
            </Button>
          ) : undefined
        }
      />

      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3 p-4">
          {isAdmin && (
            <div className="min-w-[220px]">
              <label className="mb-1 block text-[11px] font-medium text-faint">Mine view (admin)</label>
              <SimpleSelect
                aria-label="Person for Mine view"
                value={personId}
                onValueChange={setPersonId}
                options={[
                  { value: "", label: summary?.linked ? "My linked profile" : "Select a person" },
                  ...people.map((p) => ({ value: String(p.id), label: p.name })),
                ]}
              />
            </div>
          )}
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

      <Tabs value={scope} onValueChange={(v) => setScope(v as TaskScope)}>
        <TabsList className="mb-4">
          <TabsTrigger value="mine">
            Mine {summary ? <span className="ml-1.5 text-faint">{summary.mine_open}</span> : null}
          </TabsTrigger>
          {isAdmin && (
            <>
              <TabsTrigger value="unassigned">
                Unassigned {summary ? <span className="ml-1.5 text-faint">{summary.unassigned_open}</span> : null}
              </TabsTrigger>
              <TabsTrigger value="all">
                Everyone {summary ? <span className="ml-1.5 text-faint">{summary.total_open}</span> : null}
              </TabsTrigger>
            </>
          )}
        </TabsList>
      </Tabs>

      {showPersonPrompt && (
        <Card className="mb-4 border-warn/40">
          <div className="flex items-start gap-2.5 p-4 text-[13px]">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
            <div>
              <div className="font-semibold">
                {isAdmin ? "Select a person to inspect their tasks" : "Your task profile needs linking"}
              </div>
              <div className="mt-0.5 text-muted-foreground">
                {isAdmin
                  ? "Your login is not linked to a data person. Select a person in the admin-only Mine view above."
                  : "Your login is not linked to a data person, so the server cannot determine your tasks. Ask an administrator to link your account."}
              </div>
            </div>
          </div>
        </Card>
      )}

      {visibleSelectedIds.length > 0 && (
        <div className="mb-3 flex items-center gap-3 rounded-lg border border-brand/40 bg-brand/5 px-4 py-2.5">
          <ListChecks className="h-4 w-4 text-brand" />
          <span className="text-[13px] font-medium">{visibleSelectedIds.length} selected</span>
          <Button
            variant="brand"
            size="sm"
            disabled={bulkMut.isPending}
            onClick={() => bulkMut.mutate(visibleSelectedIds)}
          >
            <Check /> Mark {visibleSelectedIds.length} done
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
      {summaryQ.isError && (
        <Card className="mb-3">
          <EmptyState
            title="Could not load task totals"
            hint={getApiErrorMessage(summaryQ.error, "Refresh the page to try again.")}
          />
        </Card>
      )}
      {isAdmin && peopleQ.isError && (
        <div className="mb-3 rounded-md border border-err/40 bg-err/5 px-3 py-2 text-[12px] text-err">
          {getApiErrorMessage(peopleQ.error, "Could not load the admin person list.")}
        </div>
      )}

      {!tasksQ.isLoading && !tasksQ.isError && rows.length === 0 && (
        <Card>
          <EmptyState
            title="No open tasks 🎉"
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
                    <TH className="w-[40px]">
                      <input
                        type="checkbox"
                        aria-label={`Select all ${group.label} tasks`}
                        checked={groupAllSelected}
                        onChange={() => toggleGroup(groupIds, !groupAllSelected)}
                        className="h-3.5 w-3.5 accent-[oklch(var(--welcome-teal))]"
                      />
                    </TH>
                    <TH className="min-w-[260px]">Task</TH>
                    <TH className="min-w-[180px]">Asset</TH>
                    <TH>Reason</TH>
                    <TH className="min-w-[150px]">Assignee</TH>
                    <TH>Age</TH>
                    <TH>Created</TH>
                    <TH className="text-right">Action</TH>
                  </TR>
                </THead>
                <TBody>
                  {group.tasks.map((t) => (
                    <TR key={t.id}>
                      <TD>
                        <input
                          type="checkbox"
                          aria-label={`Select task ${t.title}`}
                          checked={selected.has(t.id)}
                          onChange={() => toggleOne(t.id)}
                          className="h-3.5 w-3.5 accent-[oklch(var(--welcome-teal))]"
                        />
                      </TD>
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
                        {fmtDateTime(t.created_at)}
                      </TD>
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
  const [kindScope, setKindScope] = useState(TASK_DEFAULT_KIND_SCOPE);
  const [scopeInitialized, setScopeInitialized] = useState(false);
  const [preview, setPreview] = useState<TaskSweepResult | null>(null);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dialogEpoch = useRef(0);

  // The reasons and scopes are backend policy — read them rather than
  // hard-coding a second copy that can drift.
  const optionsQ = useQuery({
    queryKey: ["task-generate-options"],
    queryFn: () => api.tasks.generateOptions(),
    enabled: open,
    staleTime: 60 * 60_000,
  });
  const scopes = optionsQ.data?.kind_scopes ?? [
    {
      key: TASK_DEFAULT_KIND_SCOPE,
      label: "All assets",
      hint: "All eligible assets. Unverified remains measure-only; Category may create tens of thousands of tasks.",
    },
  ];
  const currentPreviewKey = generationPreviewKey(picked, kindScope);
  const previewMatches = generationPreviewAuthorizes({
    dryRun: preview?.dry_run,
    previewKey,
    currentKey: currentPreviewKey,
    kindScope,
    previewToken: preview?.preview_token,
  });

  useEffect(() => {
    if (open && !scopeInitialized && optionsQ.data?.default_kind_scope) {
      setKindScope(optionsQ.data.default_kind_scope);
      setScopeInitialized(true);
    }
  }, [open, optionsQ.data?.default_kind_scope, scopeInitialized]);

  const previewMut = useMutation({
    mutationFn: (request: {
      reasons: string[];
      kindScope: string;
      key: string;
      epoch: number;
    }) =>
      api.tasks.generate({
        reasons: request.reasons,
        dry_run: true,
        kind_scope: request.kindScope,
      }),
    onSuccess: (r, request) => {
      if (request.epoch !== dialogEpoch.current) return;
      if (generationPreviewTokenRequired(request.kindScope) && !r.preview_token) {
        setPreview(null);
        setPreviewKey(null);
        setError(
          "The server did not authorize this broad preview. Preview again after the deployment is complete.",
        );
        return;
      }
      setPreview(r);
      setPreviewKey(request.key);
      setError(null);
    },
    onError: (e, request) => {
      if (request.epoch !== dialogEpoch.current) return;
      setError(getApiErrorMessage(e, "Preview failed."));
    },
  });
  const runMut = useMutation({
    mutationFn: (request: {
      reasons: string[];
      kindScope: string;
      previewToken?: string;
      epoch: number;
    }) =>
      api.tasks.generate(
        buildGenerationCommitRequest(
          request.reasons,
          request.kindScope,
          request.previewToken,
        ),
      ),
    onSuccess: (r, request) => {
      if (request.epoch === dialogEpoch.current) {
        setPreview(r);
        setPreviewKey(null);
        setError(null);
      }
      onDone();
    },
    onError: (e, request) => {
      if (request.epoch !== dialogEpoch.current) return;
      setPreview(null);
      setPreviewKey(null);
      setError(getApiErrorMessage(e, "Generate failed."));
    },
  });

  function toggle(key: string) {
    setPreview(null);
    setPreviewKey(null);
    setPicked((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  }

  function resetDialog() {
    dialogEpoch.current += 1;
    setPicked(REASONS.map((r) => r.key));
    setKindScope(
      optionsQ.data?.default_kind_scope ?? TASK_DEFAULT_KIND_SCOPE,
    );
    setScopeInitialized(false);
    setPreview(null);
    setPreviewKey(null);
    setError(null);
    previewMut.reset();
    runMut.reset();
  }

  function handleOpenChange(next: boolean) {
    if (!next) resetDialog();
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
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
              setPreviewKey(null);
              setScopeInitialized(true);
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
        {optionsQ.isError && (
          <div className="rounded-md border border-err/40 bg-err/5 px-3 py-2 text-[12px] text-err">
            {getApiErrorMessage(optionsQ.error, "Could not load generation options.")}
          </div>
        )}

        {preview && (!preview.dry_run || previewMatches) && (
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
            disabled={
              optionsQ.isPending ||
              previewMut.isPending ||
              runMut.isPending ||
              picked.length === 0 ||
              optionsQ.isError
            }
            onClick={() =>
              previewMut.mutate({
                reasons: [...picked],
                kindScope,
                key: currentPreviewKey,
                epoch: dialogEpoch.current,
              })
            }
          >
            Preview
          </Button>
          <Button
            variant="brand"
            size="sm"
            disabled={runMut.isPending || previewMut.isPending || !previewMatches}
            onClick={() =>
              runMut.mutate({
                reasons: [...picked],
                kindScope,
                previewToken: preview?.preview_token,
                epoch: dialogEpoch.current,
              })
            }
          >
            <Sparkles /> Generate
          </Button>
        </div>
        {!previewMatches && picked.length > 0 && (
          <p className="text-right text-[11px] text-faint">
            Run Preview for the current reasons and scope before Generate.
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}
