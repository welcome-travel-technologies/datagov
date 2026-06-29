"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, Skull, Trash2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { LoadingState, EmptyState, Spinner } from "@/components/ui/misc";
import { StatusBadge, isActiveStatus } from "@/components/integrations/status-badge";
import { fmtWhen, fmtDuration } from "@/lib/format";
import { api, type RunLog, type RunLogDetail } from "@/lib/api";

export function RunsDialog({
  open,
  onOpenChange,
  title,
  idKey,
  variant,
  logsFn,
  detailFn,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  idKey: string | number;
  /** Which backend kill/delete endpoints to hit for the rows in this dialog. */
  variant: "source" | "destination";
  logsFn: () => Promise<RunLog[]>;
  detailFn: (logId: number) => Promise<RunLogDetail>;
}) {
  const qc = useQueryClient();
  const logsKey = ["integration-runs", idKey];
  const invalidate = () => qc.invalidateQueries({ queryKey: logsKey });
  const [selected, setSelected] = useState<number | null>(null);

  const logsQuery = useQuery({
    queryKey: logsKey,
    queryFn: logsFn,
    enabled: open,
  });
  const detailQuery = useQuery({
    queryKey: ["integration-run-detail", idKey, selected],
    queryFn: () => detailFn(selected as number),
    enabled: open && selected !== null,
  });

  function close(v: boolean) {
    if (!v) setSelected(null);
    onOpenChange(v);
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {selected !== null ? (
              <button
                className="inline-flex items-center gap-1 text-foreground hover:text-brand"
                onClick={() => setSelected(null)}
              >
                <ChevronLeft className="h-4 w-4" /> Run logs — {title}
              </button>
            ) : (
              <>Run logs — {title}</>
            )}
          </DialogTitle>
        </DialogHeader>

        {selected === null ? (
          logsQuery.isLoading ? (
            <LoadingState label="Loading runs…" />
          ) : !logsQuery.data || logsQuery.data.length === 0 ? (
            <EmptyState title="No runs yet" hint="Trigger a run to see logs here." />
          ) : (
            <div className="max-h-[60vh] divide-y divide-line overflow-auto">
              {logsQuery.data.map((log) => (
                <RunRow
                  key={log.id}
                  log={log}
                  variant={variant}
                  onView={() => setSelected(log.id)}
                  onChanged={invalidate}
                />
              ))}
            </div>
          )
        ) : detailQuery.isLoading ? (
          <LoadingState label="Loading log…" />
        ) : detailQuery.data ? (
          <div className="space-y-2">
            <div className="flex items-center gap-3 text-[12.5px] text-muted-foreground">
              <StatusBadge status={detailQuery.data.status} />
              <span>{fmtWhen(detailQuery.data.started_at)}</span>
              <span className="text-faint">{detailQuery.data.triggered_by}</span>
            </div>
            <pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap rounded-lg bg-panel2 p-3 text-[12px] leading-relaxed text-foreground">
              {detailQuery.data.log_output || "(no output captured)"}
            </pre>
          </div>
        ) : (
          <EmptyState title="Log unavailable" />
        )}
      </DialogContent>
    </Dialog>
  );
}

/** One run-history row: clickable to open the log, plus Kill (only while the run
 * is active) and Delete (with confirm). The `variant` selects the source vs
 * destination backend endpoints. */
function RunRow({
  log,
  variant,
  onView,
  onChanged,
}: {
  log: RunLog;
  variant: "source" | "destination";
  onView: () => void;
  onChanged: () => void;
}) {
  const killMut = useMutation({
    mutationFn: () =>
      variant === "source"
        ? api.integrations.killRun(log.id)
        : api.integrations.killDestRun(log.id),
    onSuccess: onChanged,
  });
  const deleteMut = useMutation({
    mutationFn: () =>
      variant === "source"
        ? api.integrations.deleteRun(log.id)
        : api.integrations.deleteDestRun(log.id),
    onSuccess: onChanged,
  });

  function onDelete() {
    if (!window.confirm("Delete this run history?")) return;
    deleteMut.mutate();
  }

  return (
    <div className="flex items-center justify-between gap-3 px-1 py-2.5">
      <button onClick={onView} className="flex flex-1 items-center gap-2.5 text-left hover:opacity-80">
        <StatusBadge status={log.status} />
        <span className="text-[12.5px] text-muted-foreground">{fmtWhen(log.started_at)}</span>
      </button>
      <div className="flex items-center gap-3 text-[12px] text-faint">
        <span>{log.triggered_by}</span>
        <span>{fmtDuration(log.duration_seconds)}</span>
        {isActiveStatus(log.status) && (
          <button
            onClick={() => killMut.mutate()}
            disabled={killMut.isPending}
            title="Kill run"
            className="text-warn hover:text-warn/80 disabled:opacity-50"
          >
            {killMut.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Skull className="h-3.5 w-3.5" />}
          </button>
        )}
        <button
          onClick={onDelete}
          disabled={deleteMut.isPending}
          title="Delete run"
          className="text-err hover:text-err/80 disabled:opacity-50"
        >
          {deleteMut.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
        </button>
      </div>
    </div>
  );
}
