"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Play, FlaskConical, Settings2, ScrollText, Database, GitBranch } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Spinner } from "@/components/ui/misc";
import { StatusBadge, isActiveStatus } from "@/components/integrations/status-badge";
import { SourceDialog } from "@/components/integrations/source-dialog";
import { RunsDialog } from "@/components/integrations/runs-dialog";
import { TestResultPanel } from "@/components/integrations/test-result-panel";
import { describeSchedule } from "@/lib/integrations/schedule";
import { fmtWhen } from "@/lib/format";
import { api, getApiErrorMessage, type IntegrationSource } from "@/lib/api";

export function SourceCard({
  source,
  onChanged,
}: {
  source: IntegrationSource;
  onChanged: () => void;
}) {
  const [editOpen, setEditOpen] = useState(false);
  const [logsOpen, setLogsOpen] = useState(false);
  const isDbt = source.source_type === "dbt";
  const running = isActiveStatus(source.last_run?.status);

  const runMut = useMutation({
    mutationFn: () => api.integrations.runSource(source.id),
    onSuccess: onChanged,
  });
  const testMut = useMutation({ mutationFn: () => api.integrations.testSource(source.id) });
  const toggleMut = useMutation({
    mutationFn: (v: boolean) => api.workflow.toggleStep("source", source.id, v),
    onSuccess: onChanged,
  });

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-panel2 text-brand">
            {isDbt ? <GitBranch className="h-4 w-4" /> : <Database className="h-4 w-4" />}
          </span>
          <div>
            <div className="text-[14px] font-semibold">{source.name}</div>
            <div className="text-[12px] text-muted-foreground">
              {isDbt ? "dbt / GitHub" : "PowerBI / Fabric"}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <span>{source.is_active ? "Active" : "Inactive"}</span>
          <Switch
            checked={source.is_active}
            disabled={toggleMut.isPending}
            onCheckedChange={(v) => toggleMut.mutate(v)}
          />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12px] text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <StatusBadge status={source.last_run?.status} />
        </span>
        {source.last_run && <span>Last run {fmtWhen(source.last_run.started_at)}</span>}
        <span>Schedule: {describeSchedule(source.schedule)}</span>
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

      <SourceDialog open={editOpen} onOpenChange={setEditOpen} source={source} onSaved={onChanged} />
      <RunsDialog
        open={logsOpen}
        onOpenChange={setLogsOpen}
        title={source.name}
        idKey={`source-${source.id}`}
        variant="source"
        logsFn={() => api.integrations.sourceLogs(source.id)}
        detailFn={(logId) => api.integrations.logDetail(logId)}
      />
    </Card>
  );
}
