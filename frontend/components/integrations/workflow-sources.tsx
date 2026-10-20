"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CalendarClock, Play } from "lucide-react";
import { api, getApiErrorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { knowledgeApi, type KnowledgeSource } from "@/lib/knowledge-api";
import { sourceGroups, workflowOutputLabel } from "@/lib/integrations/source-groups";
import { SourceCard } from "./source-card";
import { DestinationCard } from "./destination-card";
import { RunList } from "@/components/knowledge/run-list";
import { KnowledgeError } from "@/components/knowledge/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LoadingState } from "@/components/ui/misc";
import { isActiveStatus } from "@/components/ui/run-status";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { useFocusGroup } from "./focus-group";

/** `focusGroup`: the group an overview link asked for (`?group=`), opened and scrolled to once. */
export function WorkflowSources({ focusGroup }: { focusGroup?: string } = {}) {
  const { user } = useAuth();
  const qc = useQueryClient();
  const changed = () => { void qc.invalidateQueries({ queryKey: ["integrations"] }); void qc.invalidateQueries({ queryKey: ["knowledge"] }); void qc.invalidateQueries({ queryKey: ["workflow-status"] }); };
  const integrations = useQuery({ queryKey: ["integrations"], queryFn: api.integrations.getAll, refetchInterval: 10000 });
  const status = useQuery({ queryKey: ["knowledge", "status", user?.id], queryFn: knowledgeApi.status });
  const enabled = !!status.data?.enabled && !!status.data?.can_manage;
  const sources = useQuery({ queryKey: ["knowledge", "sources", user?.id], queryFn: knowledgeApi.sources, enabled, refetchInterval: 10000 });
  const groups = useQuery({ queryKey: ["knowledge", "groups", user?.id], queryFn: knowledgeApi.groups, enabled, refetchInterval: 10000 });
  const runs = useQuery({ queryKey: ["knowledge", "runs", user?.id], queryFn: knowledgeApi.runs, enabled, refetchInterval: 10000 });
  const connections = useQuery({ queryKey: ["knowledge", "connections", user?.id], queryFn: knowledgeApi.connections, enabled });
  const [schedule, setSchedule] = useState<KnowledgeSource | null>(null);
  const [notice, setNotice] = useState("");
  const action = useMutation({
    mutationFn: async ({ source, patch }: { source: KnowledgeSource; patch?: { refresh_paused?: boolean; pipeline_enabled?: boolean } }) => patch ? knowledgeApi.updateSource(source.id, patch) : knowledgeApi.syncSource(source.id),
    onSuccess: (_, variables) => { changed(); setNotice(variables.patch ? "Workflow updated." : "Processing queued. Follow it in document runs below."); },
  });
  const grouped = sourceGroups(integrations.data?.sources ?? [], groups.data?.results ?? []);
  const container = useFocusGroup(focusGroup, grouped.map((group) => group.id));
  if (integrations.isPending) return <LoadingState label="Loading workflow sources…" />;
  if (integrations.isError) return <KnowledgeError message="Workflow sources could not be loaded." retry={() => { void integrations.refetch(); }} />;
  const needsAttention = runs.data?.results.some((run) => isActiveStatus(run.status) || ["failed", "partial"].includes(run.status));
  return <div ref={container} className="space-y-4">
    <div><h3 className="text-sm font-semibold">Source flows</h3><p className="mt-1 text-xs text-muted-foreground">Expand a source to manage its schedules and processing. Catalog runs and document indexing report their own progress.</p></div>
    {notice && <p role="status" className="text-sm text-brand">{notice}</p>}
    {action.isError && <KnowledgeError message={getApiErrorMessage(action.error, "The workflow could not be updated.")} />}
    {(sources.isError || groups.isError || status.isError) && <KnowledgeError message="Document workflow settings could not be loaded." retry={changed} />}
    {grouped.map((group) => <details key={group.id} data-group-key={group.id} className={cn("scroll-mt-4 rounded-xl border border-line bg-card", focusGroup === group.id && "border-brand/60 ring-1 ring-brand/30")}>
      <summary className="cursor-pointer p-4"><span className="inline-flex max-w-full flex-wrap items-center gap-x-3 gap-y-2 text-sm"><strong>{group.name}</strong><ArrowRight aria-hidden className="h-4 w-4 text-faint" /><span className="text-xs text-muted-foreground">{group.connection ? "Extract & update catalog" : group.provider === "upload" ? "Upload files" : "Read imported snapshot"}</span><ArrowRight aria-hidden className="h-4 w-4 text-faint" /><span className="text-xs text-muted-foreground">{workflowOutputLabel(group)}</span><span className="text-xs text-brand">{group.document_count.toLocaleString()} documents</span></span></summary>
      <div className="space-y-3 border-t border-line p-4">
        {group.connection && <SourceCard source={group.connection} mode="workflow" onChanged={changed} />}
        {!group.connection && group.provider !== "upload" && <p className="text-xs text-muted-foreground">This is an imported snapshot. Refresh reads its prepared manifest; it does not fetch new content from the provider.</p>}
        {sources.data?.results.filter((source) => group.collections.some((collection) => collection.id === source.id)).map((source) => {
          const supportsIndexing = !!connections.data?.results.find((connection) => connection.id === source.integration_id)?.capabilities.includes("indexing");
          return <article key={source.id} className="rounded-lg border border-line p-4">
            <div className="flex flex-wrap items-center justify-between gap-3"><div><h4 className="text-sm font-medium">{source.name}</h4><p className="mt-1 text-xs text-muted-foreground">{!source.is_active ? "Content access disabled in Sources" : source.refresh_paused ? "Processing paused" : source.pipeline_enabled ? "Runs after connector extraction" : source.cron ? `${source.cron} · ${source.timezone}` : source.kind === "upload" ? "Processes on upload" : "Manual refresh"}</p></div>
              <div className="flex flex-wrap gap-2">
                {!source.pipeline_enabled && <Button size="sm" variant="outline" onClick={() => setSchedule(source)}><CalendarClock /> Schedule</Button>}
                <Button size="sm" variant="outline" disabled={!source.is_active || action.isPending} onClick={() => action.mutate({ source, patch: { refresh_paused: !source.refresh_paused } })}>{source.refresh_paused ? "Resume processing" : "Pause processing"}</Button>
                <Button size="sm" variant="outline" disabled={!status.data?.configured || !source.is_active || source.refresh_paused || action.isPending} onClick={() => { setNotice(""); action.mutate({ source }); }}><Play />{source.pipeline_enabled ? "Retry pending indexing" : source.kind === "upload" ? "Reprocess" : "Refresh snapshot"}</Button>
              </div>
            </div>
            {source.integration_id && source.kind === "upload" && (supportsIndexing || source.pipeline_enabled) && <label className="mt-3 flex items-start gap-2 text-xs"><input type="checkbox" className="accent-brand" checked={!!source.pipeline_enabled} disabled={action.isPending} onChange={(event) => action.mutate({ source, patch: { pipeline_enabled: event.target.checked } })} />Index text definitions after connector extraction. Uses the connector schedule; replaces this collection’s independent schedule.</label>}
          </article>;
        })}
      </div>
    </details>)}
    {!!integrations.data?.destinations.length && <details className="rounded-xl border border-line bg-card"><summary className="cursor-pointer p-4 text-sm font-semibold">Destination schedules & runs</summary><div className="grid gap-3 border-t border-line p-4 lg:grid-cols-2">{integrations.data.destinations.map((destination) => <DestinationCard key={destination.id} destination={destination} mode="workflow" onChanged={changed} />)}</div></details>}
    {enabled && <details className="rounded-xl border border-line bg-card" open={needsAttention || undefined}><summary className="cursor-pointer p-4 text-sm font-semibold">Document processing runs{needsAttention ? " · Active or failed runs" : ""}</summary><div className="space-y-3 border-t border-line p-4">{runs.isPending ? <LoadingState label="Loading document runs…" /> : runs.isError ? <KnowledgeError message="Document runs could not be loaded." retry={() => { void runs.refetch(); }} /> : <RunList runs={runs.data?.results || []} />}</div></details>}
    {schedule && <CollectionSchedule source={schedule} onClose={() => setSchedule(null)} />}
  </div>;
}

export function CollectionSchedule({ source, onClose }: { source: KnowledgeSource; onClose: () => void }) {
  const [cron, setCron] = useState(source.cron);
  const [timezone, setTimezone] = useState(source.timezone);
  const qc = useQueryClient();
  const save = useMutation({ mutationFn: () => knowledgeApi.updateSource(source.id, { cron: cron.trim(), timezone: timezone.trim() }), onSuccess: () => { void qc.invalidateQueries({ queryKey: ["knowledge"] }); onClose(); } });
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent><DialogHeader><DialogTitle>Schedule: {source.name}</DialogTitle><DialogDescription>Leave the schedule blank for manual processing. Snapshot schedules reprocess the prepared import.</DialogDescription></DialogHeader>
    <label className="space-y-2 text-sm">Cron expression<Input value={cron} onChange={(event) => setCron(event.target.value)} placeholder="Manual only; e.g. 0 6 * * * for daily at 06:00" /></label>
    <label className="space-y-2 text-sm">Time zone<Input value={timezone} onChange={(event) => setTimezone(event.target.value)} /></label>
    {save.isError && <KnowledgeError message={getApiErrorMessage(save.error, "Schedule could not be saved.")} />}
    <div className="flex justify-end gap-2"><Button variant="outline" onClick={onClose}>Cancel</Button><Button variant="brand" disabled={save.isPending || !timezone.trim()} onClick={() => save.mutate()}>Save schedule</Button></div>
  </DialogContent></Dialog>;
}
