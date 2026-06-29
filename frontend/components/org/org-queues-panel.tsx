"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Layers, CalendarClock, History, Ban } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { LoadingState, EmptyState, Stat } from "@/components/ui/misc";
import { api, getApiErrorMessage, type QueuedTask, type RecentTask, type ScheduledTask } from "@/lib/api";

const QUEUES_QK = ["org-queues"];

function fmtDateTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Compact human duration, e.g. "1.4s", "3m 12s", "2h 5m". */
function fmtDuration(seconds?: number | null): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** "12s ago" / "5m ago" / "3h ago" relative to now. */
function fmtAgo(iso?: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Short module.func tail so the table cell isn't dominated by dotted paths. */
function shortFunc(func?: string | null): string {
  if (!func) return "—";
  const parts = func.split(".");
  return parts.length > 2 ? `…${parts.slice(-2).join(".")}` : func;
}

export function OrgQueuesPanel() {
  const qc = useQueryClient();
  const [detail, setDetail] = useState<RecentTask | null>(null);
  const { data, isLoading, isError, isFetching, refetch, dataUpdatedAt } = useQuery({
    queryKey: QUEUES_QK,
    queryFn: api.org.queues,
    refetchInterval: 8000,
  });

  const killMut = useMutation({
    mutationFn: (id: number) => api.org.killQueued(id),
    onSettled: () => qc.invalidateQueries({ queryKey: QUEUES_QK }),
  });

  function killTask(q: QueuedTask) {
    const label = q.name || q.func || `task #${q.id}`;
    const msg =
      q.state === "running"
        ? `Stop "${label}"? It will be signalled to cancel at its next checkpoint and removed from the queue. A step already in progress (e.g. a long extract) finishes first, then the run stops and is marked failed.`
        : `Remove "${label}" from the queue before it runs?`;
    if (window.confirm(msg)) killMut.mutate(q.id);
  }

  if (isLoading) return <LoadingState label="Reading queue…" />;
  if (isError || !data) {
    return (
      <Card>
        <CardContent className="pt-6">
          <EmptyState
            title="Couldn't read the queue"
            hint="The Django-Q queue endpoint returned an error."
          />
        </CardContent>
      </Card>
    );
  }

  const { online, clusters, counts, queued, recent, schedules } = data;
  const totalWorkers = clusters.reduce((n, c) => n + c.workers, 0);

  return (
    <div className="space-y-4">
      {/* Worker / cluster status + manual refresh */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          {online ? (
            <Badge variant="success" dot>
              {totalWorkers} worker{totalWorkers === 1 ? "" : "s"} online
            </Badge>
          ) : (
            <Badge variant="danger" dot>
              No worker running
            </Badge>
          )}
          {clusters.map((c) => (
            <Badge key={c.cluster_id} variant="outline" title={`Host ${c.host}`}>
              {c.status || "—"}
              {c.task_q_size > 0 && <span className="text-faint">· {c.task_q_size} in flight</span>}
            </Badge>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-faint">
            Updated {dataUpdatedAt ? fmtAgo(new Date(dataUpdatedAt).toISOString()) : "—"}
          </span>
          <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={isFetching ? "animate-spin" : undefined} /> Refresh
          </Button>
        </div>
      </div>

      {!online && (
        <div className="rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-[12.5px] text-warn">
          No Django-Q cluster is reporting in. Queued tasks won&apos;t run until a worker
          (<code className="font-mono">python manage.py qcluster</code>) is started.
        </div>
      )}

      {/* KPI rollups */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="In queue" value={counts.queued} accent={counts.queued > 0} />
        <Stat label="Scheduled" value={counts.scheduled} />
        <Stat label="Succeeded · 24h" value={counts.success_24h} hint={`${counts.success_total} all-time`} />
        <Stat label="Failed · 24h" value={counts.failed_24h} hint={`${counts.failed_total} all-time`} />
      </div>

      {/* In queue */}
      <Card className="overflow-hidden">
        <SectionHeader icon={<Layers className="h-3.5 w-3.5" />} title="In queue" count={queued.length} />
        {killMut.isError && (
          <div className="border-b border-err/30 bg-err/10 px-4 py-2 text-[12.5px] text-err">
            {getApiErrorMessage(killMut.error, "Couldn't remove the task.")}
          </div>
        )}
        {queued.length === 0 ? (
          <EmptyState title="Queue is empty" hint="No tasks are waiting to be picked up." />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH className="min-w-[200px]">Task</TH>
                <TH>Function</TH>
                <TH>State</TH>
                <TH>Task ID</TH>
                <TH className="text-right">Action</TH>
              </TR>
            </THead>
            <TBody>
              {queued.map((q) => (
                <TR key={q.id}>
                  <TD className="font-medium">{q.name || "—"}</TD>
                  <TD className="font-mono text-[12px] text-muted-foreground" title={q.func ?? undefined}>
                    {shortFunc(q.func)}
                  </TD>
                  <TD>
                    {q.state === "running" ? (
                      <Badge variant="info" dot>
                        Running {q.locked ? `· reserved ${fmtAgo(q.locked)}` : ""}
                      </Badge>
                    ) : (
                      <Badge variant="warning">Waiting</Badge>
                    )}
                  </TD>
                  <TD className="font-mono text-[11px] text-faint">{q.task_id || "—"}</TD>
                  <TD className="text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-err hover:bg-err/10 hover:text-err"
                      disabled={killMut.isPending && killMut.variables === q.id}
                      title={
                        q.state === "running"
                          ? "Signal this run to stop at its next checkpoint"
                          : "Remove this task from the queue before it runs"
                      }
                      onClick={() => killTask(q)}
                    >
                      <Ban /> Kill
                    </Button>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      {/* Scheduled jobs */}
      <Card className="overflow-hidden">
        <SectionHeader
          icon={<CalendarClock className="h-3.5 w-3.5" />}
          title="Scheduled jobs"
          count={schedules.length}
        />
        {schedules.length === 0 ? (
          <EmptyState title="No scheduled jobs" hint="Cron/interval schedules appear here." />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH className="min-w-[200px]">Job</TH>
                <TH>Cadence</TH>
                <TH>Next run</TH>
                <TH>Repeats</TH>
                <TH>Last run</TH>
              </TR>
            </THead>
            <TBody>
              {schedules.map((s) => (
                <TR key={s.id}>
                  <TD>
                    <div className="font-medium">{s.name || "—"}</div>
                    <div
                      className="font-mono text-[11px] text-faint"
                      title={s.func}
                    >
                      {shortFunc(s.func)}
                    </div>
                  </TD>
                  <TD>{cadence(s)}</TD>
                  <TD className="whitespace-nowrap text-[12px]">{fmtDateTime(s.next_run)}</TD>
                  <TD className="text-[12px]">{s.repeats < 0 ? "Forever" : s.repeats}</TD>
                  <TD>{lastRunBadge(s.last_success)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      {/* Recent activity */}
      <Card className="overflow-hidden">
        <SectionHeader
          icon={<History className="h-3.5 w-3.5" />}
          title="Recent activity"
          count={recent.length}
        />
        {recent.length === 0 ? (
          <EmptyState title="No completed tasks yet" />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH className="min-w-[200px]">Task</TH>
                <TH>Result</TH>
                <TH>Finished</TH>
                <TH>Duration</TH>
                <TH>Tries</TH>
              </TR>
            </THead>
            <TBody>
              {recent.map((t) => (
                <TR
                  key={t.id}
                  className="cursor-pointer"
                  title="Click to view full details"
                  onClick={() => setDetail(t)}
                >
                  <TD>
                    <div className="font-medium">{t.name || "—"}</div>
                    <div className="font-mono text-[11px] text-faint" title={t.func}>
                      {shortFunc(t.func)}
                    </div>
                  </TD>
                  <TD>{resultCell(t)}</TD>
                  <TD className="whitespace-nowrap text-[12px]">{fmtAgo(t.stopped)}</TD>
                  <TD className="text-[12px]">{fmtDuration(t.duration_seconds)}</TD>
                  <TD className="text-[12px]">{t.attempt_count || 1}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      <TaskDetailDialog task={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

function TaskDetailDialog({
  task,
  onClose,
}: {
  task: RecentTask | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={!!task} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        {task && (
          <>
            <DialogHeader>
              <DialogTitle className="break-words">{task.name || "Task"}</DialogTitle>
              <DialogDescription className="font-mono break-all">{task.func}</DialogDescription>
            </DialogHeader>

            <div className="flex flex-wrap items-center gap-2">
              {task.success ? (
                <Badge variant="success">Success</Badge>
              ) : (
                <Badge variant="danger">Failed</Badge>
              )}
              {task.group && <Badge variant="outline">{task.group}</Badge>}
            </div>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-[12.5px] sm:grid-cols-3">
              <DetailField label="Started" value={fmtDateTime(task.started)} />
              <DetailField label="Finished" value={fmtDateTime(task.stopped)} />
              <DetailField label="Duration" value={fmtDuration(task.duration_seconds)} />
              <DetailField label="Tries" value={String(task.attempt_count || 1)} />
              <DetailField label="Task ID" value={task.id} mono />
            </dl>

            <div className="space-y-1.5">
              <div className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-faint">
                {task.success ? "Response / result" : "Error"}
              </div>
              {task.result ? (
                <pre className="max-h-[45vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-panel2/50 p-3 font-mono text-[12px] leading-relaxed">
                  {task.result}
                </pre>
              ) : (
                <p className="text-[12.5px] text-faint">
                  This task did not record any result text.
                </p>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function DetailField({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-faint">{label}</dt>
      <dd className={mono ? "font-mono text-[11.5px] break-all" : undefined}>{value}</dd>
    </div>
  );
}

function SectionHeader({
  icon,
  title,
  count,
}: {
  icon: React.ReactNode;
  title: string;
  count: number;
}) {
  return (
    <div className="flex items-center gap-2 border-b border-line bg-panel2/50 px-4 py-3">
      <span className="text-faint">{icon}</span>
      <h3 className="text-[13px] font-bold uppercase tracking-wide">{title}</h3>
      <Badge>{count}</Badge>
    </div>
  );
}

function cadence(s: ScheduledTask): React.ReactNode {
  if (s.cron) return <span className="font-mono text-[12px]">{s.cron}</span>;
  if (s.schedule_type === "Minutes" && s.minutes) return <span className="text-[12px]">Every {s.minutes}m</span>;
  return <span className="text-[12px]">{s.schedule_type}</span>;
}

function lastRunBadge(success: boolean | null): React.ReactNode {
  if (success == null) return <span className="text-[12px] text-faint">Never</span>;
  return success ? <Badge variant="success">Success</Badge> : <Badge variant="danger">Failed</Badge>;
}

function resultCell(t: RecentTask): React.ReactNode {
  return (
    <div className="flex items-center gap-2">
      {t.success ? <Badge variant="success">Success</Badge> : <Badge variant="danger">Failed</Badge>}
      {t.short_result && (
        <span className="max-w-[280px] truncate text-[11.5px] text-muted-foreground" title={t.short_result}>
          {t.short_result}
        </span>
      )}
    </div>
  );
}
