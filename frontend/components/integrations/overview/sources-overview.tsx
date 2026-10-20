"use client";

import { Fragment, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, BarChart3, ChevronDown, Database, FileText, FolderInput, GitBranch, Library, MessagesSquare, Play, Plug, RefreshCw, Settings2, Upload } from "lucide-react";
import { api, getApiErrorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtWhen } from "@/lib/format";
import {
  describeCounts,
  fixLink,
  integrationsOverviewApi,
  manageLinks,
  relativeTime,
  type AdvancedView,
  type IntegrationsOverview,
  type OverviewSource,
  type RunOrderEvent,
} from "@/lib/integrations-overview-api";
import { cn } from "@/lib/utils";
import { KnowledgeError } from "@/components/knowledge/shared";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, LoadingState, Spinner } from "@/components/ui/misc";
import { StatusBadge } from "@/components/ui/run-status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

export const OVERVIEW_QK = ["integrations-overview"];

export type OverviewNavigate = (tab: AdvancedView | "content", group?: string) => void;

/** One plain-words row per source: how it runs, when next, how it went last, and its place in the run order. */
export function SourcesOverview({ onNavigate }: { onNavigate: OverviewNavigate }) {
  const { user } = useAuth();
  const [open, setOpen] = useState<string | null>(null);
  const overview = useQuery({
    queryKey: [...OVERVIEW_QK, user?.id],
    queryFn: integrationsOverviewApi.get,
    retry: false,
    refetchInterval: (query) => query.state.data?.sources.some((source) => isBusy(source.last_run.status)) ? 5000 : false,
  });
  if (overview.isPending) return <LoadingState label="Loading sources…" />;
  if (overview.isError) return <KnowledgeError message={getApiErrorMessage(overview.error, "Sources could not be loaded.")} retry={() => { void overview.refetch(); }} />;
  const data = overview.data;
  return <div className="space-y-4">
    <HowItWorks data={data} canAnalyze={!!user?.perms?.can_view_document_analyzer} />
    <p className="text-[13px] text-muted-foreground">Each source below: how it updates, when it runs next and how its last run went. Select one for details.</p>
    <RunOrder data={data} onNavigate={onNavigate} />
    <Card className="overflow-hidden">
      {data.sources.length === 0 ? <EmptyState title="No sources yet" hint="Connect dbt / GitHub or Power BI, or add documents, under Advanced › Sources." icon={<Database className="h-8 w-8" />} /> : <Table>
        <THead><tr>
          <TH>Source</TH>
          <TH>How it runs</TH>
          <TH className="hidden md:table-cell">Next run</TH>
          <TH>Last run</TH>
          <TH className="text-center" title="The order sources run in next">Order</TH>
        </tr></THead>
        <TBody>
          {data.sources.map((source) => <SourceRow key={source.key} source={source} open={open === source.key}
            onToggle={() => setOpen(open === source.key ? null : source.key)} documentsEnabled={data.documents_enabled} onNavigate={onNavigate} />)}
        </TBody>
      </Table>}
    </Card>
  </div>;
}

function isBusy(status: string) {
  return status === "running" || status === "queued";
}

/** The whole picture in four plain steps, for someone who never opens Advanced. */
function HowItWorks({ data, canAnalyze }: { data: IntegrationsOverview; canAnalyze: boolean }) {
  const names = [...new Set(data.sources.map((source) => source.name))];
  const next = data.run_order[0]?.at;
  const steps = [
    { title: "Connect", icon: <Plug />, text: names.length === 0 ? "No sources yet" : names.slice(0, 4).join(", ") + (names.length > 4 ? ` and ${names.length - 4} more` : "") },
    { title: "Update", icon: <RefreshCw />, text: next ? `Automatically, next ${relativeTime(next)}` : "When you press Run now or upload a file" },
    { title: "Organise", icon: <Library />, text: data.documents_enabled ? "Catalog definitions and searchable documents" : "Catalog definitions, reports and lineage" },
    { title: "Ask", icon: <MessagesSquare />, text: canAnalyze ? "In Chat or the Document Analyzer, with sources" : "In Chat, with sources" },
  ];
  return <ol aria-label="How DataGov works" className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
    {steps.map((step, index) => <li key={step.title} className="flex items-start gap-2.5 rounded-xl border border-line bg-card p-3">
      <span aria-hidden className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-panel2 text-brand [&_svg]:h-4 [&_svg]:w-4">{step.icon}</span>
      <span className="min-w-0">
        <span className="block text-[13px] font-semibold">{index + 1}. {step.title}</span>
        <span className="block text-[12px] text-muted-foreground">{step.text}</span>
      </span>
    </li>)}
  </ol>;
}

function SourceIcon({ source }: { source: OverviewSource }) {
  const icon = source.kind === "upload" ? <Upload /> : source.kind === "import" ? <FolderInput /> : source.provider === "dbt" ? <GitBranch />
    : source.provider === "powerbi_fabric" ? <BarChart3 /> : <Database />;
  return <span aria-hidden className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-panel2 text-brand [&_svg]:h-4 [&_svg]:w-4">{icon}</span>;
}

function SourceRow({ source, open, onToggle, documentsEnabled, onNavigate }: {
  source: OverviewSource; open: boolean; onToggle: () => void; documentsEnabled: boolean; onNavigate: OverviewNavigate;
}) {
  const detailId = `source-detail-${source.key.replace(/[^A-Za-z0-9_-]/g, "-")}`;
  return <Fragment>
    <TR className={cn("cursor-pointer", open && "bg-panel2")} onClick={onToggle}>
      <TD>
        <button type="button" aria-expanded={open} aria-controls={open ? detailId : undefined} onClick={(event) => { event.stopPropagation(); onToggle(); }}
          className="flex items-center gap-2.5 text-left">
          <ChevronDown aria-hidden className={cn("h-4 w-4 shrink-0 text-faint transition-transform", !open && "-rotate-90")} />
          <SourceIcon source={source} />
          <span className="min-w-0">
            <span className="block font-semibold">{source.name}</span>
            <span className="block text-[11.5px] text-muted-foreground">{source.provider_label}</span>
            {source.needs_attention && <Badge variant="warning" className="mt-1" title={source.attention}><AlertTriangle aria-hidden className="h-3 w-3" />Needs attention</Badge>}
          </span>
        </button>
      </TD>
      <TD className="text-muted-foreground">{source.how.text}</TD>
      <TD className="hidden whitespace-nowrap md:table-cell">
        {source.how.next_run_at ? <time dateTime={source.how.next_run_at} title={fmtWhen(source.how.next_run_at)}>{relativeTime(source.how.next_run_at)}</time> : <span className="text-faint">—</span>}
      </TD>
      <TD>
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <StatusBadge status={source.last_run.status === "never" ? null : source.last_run.status} />
          {source.last_run.at && <time dateTime={source.last_run.at} title={fmtWhen(source.last_run.at)} className="whitespace-nowrap text-[12px] text-muted-foreground">{relativeTime(source.last_run.at)}</time>}
        </span>
      </TD>
      <TD className="text-center font-semibold">
        {source.order ? <span>#{source.order}</span> : <span className="font-normal text-faint" title="No upcoming run">—</span>}
      </TD>
    </TR>
    {open && <tr id={detailId}>
      <td colSpan={5} className="bg-panel2 px-[18px] pb-4 pt-1">
        <SourceDetail source={source} detailId={detailId} documentsEnabled={documentsEnabled} onNavigate={onNavigate} />
      </td>
    </tr>}
  </Fragment>;
}

function SourceDetail({ source, detailId, documentsEnabled, onNavigate }: {
  source: OverviewSource; detailId: string; documentsEnabled: boolean; onNavigate: OverviewNavigate;
}) {
  const qc = useQueryClient();
  // Same call as the connector card's "Run now"; only connectors can be run from here.
  const run = useMutation({
    mutationFn: () => api.integrations.runSource(source.connection_id as number),
    onSuccess: () => {
      for (const queryKey of [OVERVIEW_QK, ["integrations"], ["workflow-status"]]) void qc.invalidateQueries({ queryKey });
    },
  });
  const fix = fixLink(source);
  const busy = isBusy(source.last_run.status);
  const attentionId = `${detailId}-attention`;
  const heading = "text-[10.5px] font-semibold uppercase tracking-[0.06em] text-faint";
  return <div className="space-y-4 rounded-lg border border-line bg-card p-4 text-[13px]">
    <div className="grid gap-4 md:grid-cols-3">
      <section><h4 className={heading}>What it brings</h4><p className="mt-1">{describeCounts(source.counts)}</p></section>
      <section>
        <h4 className={heading}>How it updates</h4>
        <p className="mt-1 text-muted-foreground">{source.detail.updates}</p>
        {source.workflow_step && <p className="mt-1 text-muted-foreground">Step {source.workflow_step} of the catalog workflow.</p>}
      </section>
      <section>
        <h4 className={heading}>What happens when it runs</h4>
        <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-muted-foreground">{source.detail.steps.map((step) => <li key={step}>{step}</li>)}</ol>
      </section>
    </div>
    {source.attention && <p id={attentionId} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-warn">
      <AlertTriangle aria-hidden className="h-4 w-4 shrink-0" />{source.attention}
      {fix && <Button size="sm" variant="link" className="h-auto px-0" onClick={() => onNavigate(fix.tab, fix.group)}>Fix in {fix.label.toLowerCase()}</Button>}
    </p>}
    <div className="flex flex-wrap items-center gap-2">
      {/* Disabled buttons cannot show a tooltip, so the label (or the visible reason above) says why. */}
      {source.kind === "connector" && source.connection_id !== null && <Button size="sm" variant="brand" disabled={!source.can_run_now || run.isPending} onClick={() => run.mutate()}
        aria-describedby={source.run_blocked && source.attention ? attentionId : undefined}>
        {run.isPending || busy ? <Spinner className="text-white" /> : <Play />}
        {!source.is_active ? "Activate to run" : busy ? "Running…" : "Run now"}
      </Button>}
      {source.run_blocked && source.run_blocked !== source.attention && <span className="text-[12.5px] text-muted-foreground">{source.run_blocked}</span>}
      <span className="text-[12px] text-faint">Manage:</span>
      {manageLinks(source).map((link) => <Button key={link.tab} size="sm" variant="outline" onClick={() => onNavigate(link.tab, link.group)}><Settings2 /> {link.label}</Button>)}
      {documentsEnabled && !!source.counts.visible_documents && <Button size="sm" variant="ghost" onClick={() => onNavigate("content", source.key)}><FileText /> View documents</Button>}
      {run.isSuccess && <span role="status" className="text-[12.5px] text-brand">Run started.</span>}
      {run.isError && <span role="alert" className="text-[12.5px] text-err">{getApiErrorMessage(run.error, "Could not start the run.")}</span>}
    </div>
  </div>;
}

type TimeBlock = { at: string; workflow: string[]; workflowRepeat: string | null; other: { label: string; repeat: string | null }[] };

/** Events at the same moment form one block; catalog workflow steps keep their run order. */
function blocksByTime(events: RunOrderEvent[]): TimeBlock[] {
  const blocks: TimeBlock[] = [];
  for (const event of events) {
    let block = blocks[blocks.length - 1];
    if (!block || new Date(block.at).getTime() !== new Date(event.at).getTime()) {
      block = { at: event.at, workflow: [], workflowRepeat: null, other: [] };
      blocks.push(block);
    }
    if (event.kind === "workflow") {
      block.workflow.push(event.label);
      block.workflowRepeat = event.repeat ?? null;
    } else block.other.push({ label: event.label, repeat: event.repeat ?? null });
  }
  return blocks;
}

function timeLabel(iso: string) {
  const date = new Date(iso);
  const time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return date.toDateString() === new Date().toDateString() ? time : `${date.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
}

function Repeats({ repeat }: { repeat: string | null }) {
  return repeat ? <span className="block text-[11px] text-faint">Repeats {repeat}</span> : null;
}

function RunOrder({ data, onNavigate }: { data: IntegrationsOverview; onNavigate: OverviewNavigate }) {
  const blocks = blocksByTime(data.run_order);
  const workflow = data.workflow;
  return <section aria-label="Run order" className="rounded-xl border border-line bg-card p-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h3 className="text-sm font-semibold">Run order · next 24 hours</h3>
      <span className="text-[11.5px] text-faint">Times in your time zone</span>
    </div>
    {blocks.length === 0 ? <p className="mt-2 text-[12.5px] text-muted-foreground">Nothing is scheduled in the next 24 hours.</p> : <ol className="mt-3 flex flex-wrap gap-2">
      {blocks.map((block) => <li key={block.at} className="max-w-full rounded-lg border border-line bg-panel2 px-3 py-2 text-[12.5px]">
        <time dateTime={block.at} title={fmtWhen(block.at)} className="block text-[11px] font-semibold text-brand">{timeLabel(block.at)}</time>
        {block.workflow.length > 0 && <><span className="block">Catalog workflow: {block.workflow.join(" → ")}</span><Repeats repeat={block.workflowRepeat} /></>}
        {block.other.map((item, index) => <Fragment key={`${index}-${item.label}`}><span className="block">{item.label}</span><Repeats repeat={item.repeat} /></Fragment>)}
      </li>)}
    </ol>}
    {data.run_order_truncated && <p className="mt-2 text-[11.5px] text-faint">Showing the first runs only.</p>}
    <div className="mt-3 space-y-1.5 border-t border-line pt-3 text-[12.5px]">
      <p className="flex flex-wrap items-center gap-2">
        <span className="font-medium">Catalog workflow</span><span className="text-muted-foreground">· {workflow.how.text}</span>
        {workflow.how.next_run_at && <span className="text-muted-foreground">· next {relativeTime(workflow.how.next_run_at)}</span>}
        <StatusBadge status={workflow.last_run.status === "never" ? null : workflow.last_run.status} />
        <Button size="sm" variant="outline" className="ml-auto" onClick={() => onNavigate("workflow")}><Settings2 /> Manage workflow</Button>
      </p>
      <ol aria-label="Catalog workflow order" className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-muted-foreground">
        {workflow.steps.map((step, index) => <li key={`${step.order}-${step.label}`} className="inline-flex items-center gap-1.5">
          {index > 0 && <ArrowRight aria-hidden className="h-3 w-3 text-faint" />}
          <span className="rounded bg-panel2 px-1.5 text-[11px] font-semibold text-foreground">{step.order}</span>{step.label}
        </li>)}
      </ol>
      <p className="text-[11.5px] text-faint">{workflow.note}</p>
      {workflow.attention && <p className="flex items-center gap-2 text-warn"><AlertTriangle aria-hidden className="h-4 w-4 shrink-0" />{workflow.attention}</p>}
    </div>
  </section>;
}
