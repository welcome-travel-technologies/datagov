"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarClock,
  ScrollText,
  Settings2,
  ChevronDown,
  FlaskConical,
  Save,
  Trash2,
  Skull,
  Eraser,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Field } from "@/components/ui/form-field";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Spinner, LoadingState } from "@/components/ui/misc";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { StatusBadge, isActiveStatus } from "@/components/integrations/status-badge";
import { WorkflowDag } from "@/components/integrations/workflow-dag";
import { ScheduleFields } from "@/components/integrations/schedule-fields";
import { TestResultPanel } from "@/components/integrations/test-result-panel";
import {
  describeSchedule,
  scheduleStateFrom,
  type ScheduleState,
} from "@/lib/integrations/schedule";
import { fmtWhen, fmtDuration } from "@/lib/format";
import { api, getApiErrorMessage, type WorkflowStatus, type WorkflowRun } from "@/lib/api";
import { cn } from "@/lib/utils";

const QK = ["workflow-status"];

export function WorkflowPanel() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: QK });

  const statusQuery = useQuery({
    queryKey: QK,
    queryFn: api.workflow.status,
    refetchInterval: (q) => {
      const runs = q.state.data?.runs;
      return runs && runs.length > 0 && isActiveStatus(runs[0].status) ? 4000 : false;
    },
  });

  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [detailRun, setDetailRun] = useState<number | null>(null);

  if (statusQuery.isLoading) return <LoadingState label="Loading workflow…" />;
  const data = statusQuery.data;
  if (!data) return null;

  const latest = data.runs[0];
  const running = isActiveStatus(latest?.status);

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="pt-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-[14px] font-semibold">Full pipeline</h3>
              <p className="mt-0.5 text-[12.5px] text-muted-foreground">
                Runs all active transformation sources, then visualization sources, then pushes to
                destinations. Schedule: <span className="font-medium text-foreground">{describeSchedule(data.schedule)}</span>
              </p>
            </div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setScheduleOpen(true)}>
                <CalendarClock /> Schedule
              </Button>
            </div>
          </div>

          {/* Staged ETL pipeline DAG */}
          <div className="mt-4">
            <WorkflowDag data={data} onViewLog={(id) => setDetailRun(id)} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-5">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h3 className="text-[14px] font-semibold">Recent runs</h3>
            <div className="flex items-center gap-3">
              {running && (
                <span className="flex items-center gap-1.5 text-[12px] text-run">
                  <Spinner /> {latest?.current_stage ?? "running"}
                </span>
              )}
              <CleanLogsButton onCleaned={invalidate} />
            </div>
          </div>
          {data.runs.length === 0 ? (
            <p className="py-4 text-center text-[12.5px] text-muted-foreground">No workflow runs yet.</p>
          ) : (
            <div className="divide-y divide-line">
              {data.runs.map((r) => (
                <WorkflowRunRow
                  key={r.id}
                  run={r}
                  onView={() => setDetailRun(r.id)}
                  onChanged={invalidate}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <RawExportCard rawExport={data.raw_export} onSaved={invalidate} />

      <WorkflowScheduleDialog
        open={scheduleOpen}
        onOpenChange={setScheduleOpen}
        initial={scheduleStateFrom(data.schedule)}
        onSaved={invalidate}
      />
      <WorkflowRunDetailDialog runId={detailRun} onClose={() => setDetailRun(null)} />
    </div>
  );
}

function WorkflowScheduleDialog({
  open,
  onOpenChange,
  initial,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  initial: ScheduleState;
  onSaved: () => void;
}) {
  const [schedule, setSchedule] = useState<ScheduleState>(initial);
  useEffect(() => {
    if (open) setSchedule(initial);
  }, [open, initial]);

  const saveMut = useMutation({
    mutationFn: () =>
      api.workflow.saveSchedule({
        frequency: schedule.frequency,
        schedule_enabled: schedule.enabled,
        cron_expression: schedule.cron,
        schedule_hour: schedule.hour,
        schedule_day: schedule.day,
      }),
    onSuccess: () => {
      onSaved();
      onOpenChange(false);
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Workflow schedule</DialogTitle>
          <DialogDescription>Run the full pipeline automatically on a schedule.</DialogDescription>
        </DialogHeader>
        <ScheduleFields value={schedule} onChange={setSchedule} />
        {saveMut.isError && (
          <p className="text-[12.5px] text-err">
            {getApiErrorMessage(saveMut.error, "Could not save the schedule.")}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="brand" onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
            {saveMut.isPending ? "Saving…" : "Save schedule"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function WorkflowRunDetailDialog({ runId, onClose }: { runId: number | null; onClose: () => void }) {
  const detail = useQuery({
    queryKey: ["workflow-run-detail", runId],
    queryFn: () => api.workflow.runDetail(runId as number),
    enabled: runId !== null,
    refetchInterval: (q) => (isActiveStatus(q.state.data?.status) ? 3000 : false),
  });

  return (
    <Dialog open={runId !== null} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Workflow run #{runId}</DialogTitle>
        </DialogHeader>
        {detail.isLoading ? (
          <LoadingState label="Loading run…" />
        ) : detail.data ? (
          <div className="space-y-2">
            <div className="flex items-center gap-3 text-[12.5px] text-muted-foreground">
              <StatusBadge status={detail.data.status} />
              <span>{fmtWhen(detail.data.started_at)}</span>
              <span className="text-faint">{detail.data.triggered_by}</span>
            </div>
            <pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap rounded-lg bg-panel2 p-3 text-[12px] leading-relaxed">
              {detail.data.log_output || "(no output captured)"}
            </pre>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

/** One recent-run row with a clickable area (opens the log) plus Kill (only
 * while running/pending) and Delete (with confirm) actions. */
function WorkflowRunRow({
  run,
  onView,
  onChanged,
}: {
  run: WorkflowRun;
  onView: () => void;
  onChanged: () => void;
}) {
  const killMut = useMutation({
    mutationFn: () => api.workflow.killRun(run.id),
    onSuccess: onChanged,
  });
  const deleteMut = useMutation({
    mutationFn: () => api.workflow.deleteRun(run.id),
    onSuccess: onChanged,
  });
  const canKill = run.status === "running" || run.status === "pending";

  function onDelete() {
    if (!window.confirm("Delete this workflow run history?")) return;
    deleteMut.mutate();
  }

  return (
    <div className="flex items-center justify-between gap-3 py-2.5">
      <button onClick={onView} className="flex flex-1 items-center gap-2.5 text-left">
        <StatusBadge status={run.status} />
        <span className="text-[12.5px] text-muted-foreground">{fmtWhen(run.started_at)}</span>
      </button>
      <div className="flex items-center gap-3 text-[12px] text-faint">
        <span>{run.triggered_by}</span>
        <span>{fmtDuration(run.duration_seconds)}</span>
        <button
          onClick={onView}
          title="View log"
          className="text-faint hover:text-brand"
        >
          <ScrollText className="h-3.5 w-3.5" />
        </button>
        {canKill && (
          <button
            onClick={() => killMut.mutate()}
            disabled={killMut.isPending}
            title="Kill run"
            className="text-warn hover:text-warn/80 disabled:opacity-50"
          >
            <Skull className="h-3.5 w-3.5" />
          </button>
        )}
        <button
          onClick={onDelete}
          disabled={deleteMut.isPending}
          title="Delete run"
          className="text-err hover:text-err/80 disabled:opacity-50"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

/** Delete every run log (source, destination, workflow) for the org. */
function CleanLogsButton({ onCleaned }: { onCleaned: () => void }) {
  const mut = useMutation({
    mutationFn: () => api.integrations.cleanLogs(),
    onSuccess: onCleaned,
  });
  function onClick() {
    if (!window.confirm("Delete ALL run logs for this organization? This cannot be undone.")) return;
    mut.mutate();
  }
  return (
    <button
      onClick={onClick}
      disabled={mut.isPending}
      className="inline-flex items-center gap-1.5 text-[12px] font-medium text-faint hover:text-err disabled:opacity-50"
      title="Delete all run logs"
    >
      {mut.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Eraser className="h-3.5 w-3.5" />} Clean logs
    </button>
  );
}

/** Advanced workflow setting: zip each source's raw extract and upload to GCS. */
function RawExportCard({
  rawExport,
  onSaved,
}: {
  rawExport: WorkflowStatus["raw_export"];
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(rawExport.is_active);
  const [bucket, setBucket] = useState(rawExport.gcs_bucket_name);
  const [saJson, setSaJson] = useState("");

  useEffect(() => {
    setActive(rawExport.is_active);
    setBucket(rawExport.gcs_bucket_name);
    setSaJson("");
  }, [rawExport.is_active, rawExport.gcs_bucket_name]);

  const saveMut = useMutation({
    mutationFn: () =>
      api.workflow.saveRawExport({
        is_active: active,
        gcs_bucket_name: bucket.trim(),
        ...(saJson.trim() ? { gcs_service_account_json: saJson } : {}),
      }),
    onSuccess: () => {
      setSaJson("");
      onSaved();
    },
  });

  const testMut = useMutation({
    mutationFn: () =>
      api.workflow.testRawExport({
        gcs_bucket_name: bucket.trim(),
        ...(saJson.trim() ? { gcs_service_account_json: saJson } : {}),
      }),
  });

  return (
    <Card>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-5 py-3.5 text-left"
      >
        <span className="flex items-center gap-2 text-[14px] font-semibold">
          <Settings2 className="h-4 w-4 text-brand" /> Advanced settings
          {rawExport.is_active && (
            <Badge variant="success" dot>
              Raw Export Active
            </Badge>
          )}
        </span>
        <ChevronDown className={cn("h-4 w-4 text-faint transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <CardContent className="space-y-4 border-t border-line pt-4">
          <div>
            <h4 className="text-[13px] font-semibold">Save raw source exports to Google Cloud Storage</h4>
            <p className="mt-1 text-[12px] text-muted-foreground">
              When enabled, every active source&apos;s raw extracted files are zipped after extraction
              and uploaded to your bucket — one zip per source per workflow run.
            </p>
          </div>

          <div className="flex items-center justify-between">
            <span className="text-[12.5px] font-medium">Active</span>
            <Switch checked={active} onCheckedChange={setActive} />
          </div>

          <Field label="GCS bucket name">
            <Input
              value={bucket}
              onChange={(e) => setBucket(e.target.value)}
              placeholder="my-raw-exports-bucket"
              className="font-mono"
            />
          </Field>

          <Field
            label={
              rawExport.gcs_service_account_set
                ? "Service account JSON (set — blank keeps it)"
                : "Service account JSON"
            }
          >
            <textarea
              value={saJson}
              onChange={(e) => setSaJson(e.target.value)}
              rows={5}
              placeholder={rawExport.gcs_service_account_set ? "••• saved" : "Paste full GCP service account JSON"}
              className={cn(
                "flex w-full rounded-md border border-input bg-panel px-3 py-2 font-mono text-[12px] shadow-sm",
                "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              )}
            />
          </Field>

          <div className="flex items-center gap-2">
            <Button variant="brand" size="sm" onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
              {saveMut.isPending ? <Spinner className="text-white" /> : <Save />} Save
            </Button>
            <Button variant="outline" size="sm" onClick={() => testMut.mutate()} disabled={testMut.isPending}>
              {testMut.isPending ? <Spinner /> : <FlaskConical />} Test Connection
            </Button>
          </div>

          {saveMut.isError && (
            <p className="text-[12.5px] text-err">
              {getApiErrorMessage(saveMut.error, "Could not save raw export.")}
            </p>
          )}

          <TestResultPanel
            pending={testMut.isPending}
            result={testMut.data}
            error={getApiErrorMessage(testMut.error, testMut.isError ? "Test failed." : null)}
          />
        </CardContent>
      )}
    </Card>
  );
}
