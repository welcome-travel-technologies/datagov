"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { useState } from "react";
import { CircleStop, Clock3, RotateCcw } from "lucide-react";
import { getApiErrorMessage } from "@/lib/api";
import { knowledgeApi, type KnowledgeRun } from "@/lib/knowledge-api";
import { Button } from "@/components/ui/button";
import { isActiveStatus } from "@/components/ui/run-status";
import { EmptyState } from "@/components/ui/misc";
import { KnowledgeError, StatusBadge, displayDate, readableStatus } from "./shared";

export function RunList({ runs }: { runs: KnowledgeRun[] }) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [limit, setLimit] = useState(10);
  const outputs = useQuery({ queryKey: ["knowledge", "indexing-outputs", user?.id], queryFn: knowledgeApi.indexingOutputs, retry: false, refetchInterval: 10000 });
  const action = useMutation({
    mutationFn: ({ id, cancel }: { id: string; cancel: boolean }) => cancel ? knowledgeApi.cancelRun(id) : knowledgeApi.retryRun(id),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["knowledge"] }); },
  });
  return <div className="space-y-3">
    <p className="text-[13px] text-muted-foreground">Follow document processing, retry failed runs, and request cancellation. Published documents remain available while a refresh runs.</p>
    {action.isError && <KnowledgeError message={getApiErrorMessage(action.error, "The run could not be updated.")} />}
    {outputs.data?.results?.filter((output) => !output.run_id).map((output) => <article key={output.id} className="rounded-xl border border-line bg-card p-4">
      <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">{output.source_name}</h3><StatusBadge status={output.status} /></div>
      <p className="mt-2 text-xs text-muted-foreground">{output.upstream_key} · {output.profile} · {displayDate(output.created_at)}</p>
      <p className="mt-2 text-xs">{output.error || "Extraction saved. Indexing will start when this collection is available."}</p>
    </article>)}
    {!runs.length && <EmptyState title="No processing runs yet" hint="Upload a PDF or refresh a source to start a run." icon={<Clock3 className="h-8 w-8" />} />}
    {runs.slice(0, limit).map((run) => {
      const active = isActiveStatus(run.status);
      const failed = ["failed", "partial", "cancelled", "error"].includes(run.status);
      const progress = Math.max(0, Math.min(100, Number(run.progress) || 0));
      return <article key={run.id} className="rounded-xl border border-line bg-card p-4">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1"><h3 className="text-[13px] font-semibold">{run.source_name}</h3><p className="mt-1 text-xs text-muted-foreground">{displayDate(run.created_at)}{run.stage ? ` · ${readableStatus(run.stage)}` : ""}</p></div>
          <StatusBadge status={run.cancel_requested && active ? "cancel_requested" : run.status} />
          {active && <Button variant="outline" size="sm" disabled={action.isPending || run.cancel_requested} onClick={() => action.mutate({ id: run.id, cancel: true })}><CircleStop />{run.cancel_requested ? "Cancelling…" : "Cancel"}</Button>}
          {failed && <Button variant="outline" size="sm" disabled={action.isPending} onClick={() => action.mutate({ id: run.id, cancel: false })}><RotateCcw /> Retry</Button>}
        </div>
        {run.upstream_key && <p className="mt-2 text-xs text-muted-foreground">Integration extraction {run.upstream_key} · {run.profile}</p>}
        {active && <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-panel2" role="progressbar" aria-label={`Processing ${run.source_name}`} aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}><div className="h-full rounded-full bg-brand transition-all" style={{ width: `${progress}%` }} /></div>}
        {!!Object.keys(run.counters || {}).length && <dl className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">{Object.entries(run.counters).map(([key, count]) => <div key={key}><dt className="inline">{readableStatus(key)}: </dt><dd className="inline font-medium text-foreground">{count.toLocaleString()}</dd></div>)}</dl>}
        {run.error && <p className="mt-3 whitespace-pre-wrap break-words rounded-lg bg-err/5 p-3 text-xs text-err">{run.error}</p>}
      </article>;
    })}
    {runs.length > limit && <Button variant="outline" onClick={() => setLimit((value) => value + 10)}>Show more runs ({runs.length - limit})</Button>}
  </div>;
}
