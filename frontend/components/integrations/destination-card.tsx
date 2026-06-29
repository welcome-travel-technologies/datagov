"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Play, FlaskConical, Settings2, ScrollText, CloudUpload } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Spinner } from "@/components/ui/misc";
import { StatusBadge, isActiveStatus } from "@/components/integrations/status-badge";
import { DestinationDialog } from "@/components/integrations/destination-dialog";
import { RunsDialog } from "@/components/integrations/runs-dialog";
import { TestResultPanel } from "@/components/integrations/test-result-panel";
import { describeSchedule } from "@/lib/integrations/schedule";
import { fmtWhen } from "@/lib/format";
import { api, getApiErrorMessage, type IntegrationDestination } from "@/lib/api";

export function DestinationCard({
  destination,
  onChanged,
}: {
  destination: IntegrationDestination;
  onChanged: () => void;
}) {
  const [editOpen, setEditOpen] = useState(false);
  const [logsOpen, setLogsOpen] = useState(false);
  const running = isActiveStatus(destination.last_run?.status);

  const runMut = useMutation({
    mutationFn: () => api.integrations.runDestination(destination.id),
    onSuccess: onChanged,
  });
  const testMut = useMutation({ mutationFn: () => api.integrations.testDestination(destination.id) });
  const toggleMut = useMutation({
    mutationFn: (v: boolean) => api.workflow.toggleStep("destination", destination.id, v),
    onSuccess: onChanged,
  });

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-panel2 text-brand">
            <CloudUpload className="h-4 w-4" />
          </span>
          <div>
            <div className="text-[14px] font-semibold">{destination.name}</div>
            <div className="text-[12px] text-muted-foreground">
              BigQuery{destination.bq_dataset_id ? ` · ${destination.bq_dataset_id}` : ""}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <span>{destination.is_active ? "Active" : "Inactive"}</span>
          <Switch
            checked={destination.is_active}
            disabled={toggleMut.isPending}
            onCheckedChange={(v) => toggleMut.mutate(v)}
          />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12px] text-muted-foreground">
        <StatusBadge status={destination.last_run?.status} />
        {destination.last_run && <span>Last run {fmtWhen(destination.last_run.started_at)}</span>}
        <span>Schedule: {describeSchedule(destination.schedule)}</span>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="brand"
          onClick={() => runMut.mutate()}
          disabled={runMut.isPending || running}
        >
          {runMut.isPending || running ? <Spinner className="text-white" /> : <Play />}
          {running ? "Running…" : "Run now"}
        </Button>
        <Button size="sm" variant="outline" onClick={() => testMut.mutate()} disabled={testMut.isPending}>
          {testMut.isPending ? <Spinner /> : <FlaskConical />} Test
        </Button>
        <Button size="sm" variant="outline" onClick={() => setEditOpen(true)}>
          <Settings2 /> Configure
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setLogsOpen(true)}>
          <ScrollText /> Logs
        </Button>
      </div>

      {runMut.isError && (
        <p className="mt-2 text-[12.5px] text-err">
          {getApiErrorMessage(runMut.error, "Could not start run.")}
        </p>
      )}
      <TestResultPanel
        pending={testMut.isPending}
        result={testMut.data}
        error={getApiErrorMessage(testMut.error, testMut.isError ? "Test failed." : null)}
      />

      <DestinationDialog
        open={editOpen}
        onOpenChange={setEditOpen}
        destination={destination}
        onSaved={onChanged}
      />
      <RunsDialog
        open={logsOpen}
        onOpenChange={setLogsOpen}
        title={destination.name}
        idKey={`dest-${destination.id}`}
        variant="destination"
        logsFn={() => api.integrations.destLogs(destination.id)}
        detailFn={(logId) => api.integrations.destLogDetail(logId)}
      />
    </Card>
  );
}
