"use client";

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Zap,
  RefreshCw,
  Play,
  Square,
  RotateCw,
  Check,
  X,
  Circle,
  Database,
  GitBranch,
  ScrollText,
} from "lucide-react";
import { Spinner } from "@/components/ui/misc";
import { Switch } from "@/components/ui/switch";
import { StatusBadge, isActiveStatus } from "@/components/integrations/status-badge";
import { fmtWhen } from "@/lib/format";
import {
  api,
  type IntegrationsData,
  type WorkflowStatus,
  type WorkflowStepSummary,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/* ────────────────────────────────────────────────────────────────────────────
   Source/destination type metadata (icon + human label). Mirrors the original
   SOURCE_META / DEST_META maps.
──────────────────────────────────────────────────────────────────────────── */

type StepTypeMeta = { label: string; icon: React.ReactNode };

const SOURCE_META: Record<string, StepTypeMeta> = {
  powerbi_fabric: { label: "PowerBI / Microsoft Fabric API", icon: <Database className="h-4 w-4" /> },
  dbt: { label: "dbt (Data Build Tool)", icon: <GitBranch className="h-4 w-4" /> },
};

function sourceMeta(sourceType?: string): StepTypeMeta {
  if (sourceType && SOURCE_META[sourceType]) return SOURCE_META[sourceType];
  return { label: sourceType || "Source", icon: <Database className="h-4 w-4" /> };
}

const DEST_META: Record<string, StepTypeMeta> = {
  bigquery: { label: "Google BigQuery", icon: <Database className="h-4 w-4" /> },
};

function destMeta(destType?: string): StepTypeMeta {
  if (destType && DEST_META[destType]) return DEST_META[destType];
  return { label: destType || "Destination", icon: <Database className="h-4 w-4" /> };
}

/* ────────────────────────────────────────────────────────────────────────────
   Stage state — copied 1:1 from the original `_stageState` (lines 219–227).
──────────────────────────────────────────────────────────────────────────── */

type StageId = "init" | "sources" | "final" | "destinations";
type StageState = "idle" | "running" | "done" | "failed";

const STAGES: StageId[] = ["init", "sources", "final", "destinations"];

function stageState(
  stage: StageId,
  currentStage: string,
  status: string,
  hasRun: boolean,
): StageState {
  if (!hasRun) return "idle";
  const si = STAGES.indexOf(stage);
  const ci = STAGES.indexOf(currentStage as StageId);
  if (status === "success") return "done";
  if (status === "failed") return si <= ci ? (si === ci ? "failed" : "done") : "idle";
  if (si < ci) return "done";
  if (si === ci) return "running";
  return "idle";
}

/* Running-pill label for the current stage. */
const STAGE_LABEL: Record<string, string> = {
  init: "Prepare",
  sources: "Sources",
  final: "Finish",
  destinations: "Destinations",
  done: "Complete",
};

/* ────────────────────────────────────────────────────────────────────────────
   Small presentational helpers
──────────────────────────────────────────────────────────────────────────── */

/** Status dot for the small per-node last-status indicator. */
function statusDotClass(lastStatus: string | null): string {
  if (lastStatus === "success") return "bg-ok";
  if (lastStatus === "failed") return "bg-err";
  return "bg-faint/40";
}

/** Dashed arrow connector between DAG columns (matches the original layout: an
 *  invisible title placeholder so the arrow sits on the shared body centerline). */
function ArrowCol() {
  return (
    <div className="flex flex-shrink-0 flex-col" aria-hidden="true">
      <p className="invisible mb-2.5 text-center text-[10px] font-bold uppercase tracking-wider">·</p>
      <div className="flex flex-1 items-center justify-center">
        <div className="flex items-center px-1">
          <svg className="h-6 w-10 flex-shrink-0 text-line-strong" viewBox="0 0 40 24" fill="none">
            <line
              x1="3"
              y1="12"
              x2="30"
              y2="12"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeDasharray="4 4"
              strokeLinecap="round"
            />
            <path
              d="M28 7l8 5-8 5"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
      </div>
    </div>
  );
}

/** A bare INIT/FINAL stage box (Prepare / Finish). */
function StageBox({ label, state }: { label: string; state: StageState }) {
  const boxCls =
    state === "running"
      ? "border-run/50 bg-run/[0.06] ring-2 ring-run/25 shadow-sm"
      : state === "done"
        ? "border-ok/40 bg-ok/[0.07]"
        : state === "failed"
          ? "border-err/40 bg-err/[0.07]"
          : "border-line bg-card";

  let icon: React.ReactNode;
  if (state === "running") {
    icon = (
      <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-run/15">
        <Spinner className="h-5 w-5 text-run" />
      </div>
    );
  } else if (state === "done") {
    icon = (
      <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-ok/15">
        <Check className="h-5 w-5 text-ok" strokeWidth={2.5} />
      </div>
    );
  } else if (state === "failed") {
    icon = (
      <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-err/15">
        <X className="h-5 w-5 text-err" strokeWidth={2.5} />
      </div>
    );
  } else {
    icon = (
      <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-panel2">
        <Circle className="h-4 w-4 text-faint/60" strokeWidth={2} />
      </div>
    );
  }

  return (
    <div className="flex flex-shrink-0 flex-col">
      <p className="mb-2.5 text-center text-[10px] font-bold uppercase tracking-wider text-faint">
        {label === "Prepare" ? "Initialize" : "Finalize"}
      </p>
      <div className="flex flex-1 items-center justify-center">
        <div
          className={cn(
            "w-[112px] rounded-2xl border px-4 py-4 text-center transition-all duration-300 hover:-translate-y-0.5",
            boxCls,
          )}
        >
          <div className="mb-2.5 flex justify-center">{icon}</div>
          <p className="text-xs font-bold uppercase tracking-wide text-foreground/80">{label}</p>
        </div>
      </div>
    </div>
  );
}

/** Credentials badge: ✓ (green) when present, ⚠ (amber) otherwise. */
function CredsBadge({ has }: { has: boolean }) {
  return has ? (
    <span className="ml-0.5 text-[10px] font-bold text-ok" title="Credentials configured">
      ✓
    </span>
  ) : (
    <span className="ml-0.5 text-[10px] text-warn" title="Missing credentials">
      ⚠
    </span>
  );
}

/* ────────────────────────────────────────────────────────────────────────────
   Source / destination DAG node
──────────────────────────────────────────────────────────────────────────── */

function StepNode({
  step,
  kind,
  meta,
  hasCreds,
  highlighted,
  onToggle,
  toggling,
}: {
  step: WorkflowStepSummary;
  kind: "source" | "destination";
  meta: StepTypeMeta;
  hasCreds: boolean;
  highlighted: boolean;
  onToggle: (checked: boolean) => void;
  toggling: boolean;
}) {
  const ring = highlighted
    ? "ring-2 ring-run/25 border-run/40 bg-run/[0.04] shadow-sm"
    : step.is_active
      ? "border-line bg-card hover:shadow-card"
      : "border-dashed border-line bg-panel2";

  return (
    <div
      className={cn(
        "rounded-2xl border px-3.5 py-3 shadow-card transition-all duration-200 hover:-translate-y-0.5",
        ring,
        !step.is_active && "opacity-50",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <div
            className={cn(
              "flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-xl",
              kind === "source" ? "bg-brand/10 text-brand" : "bg-warn/15 text-warn",
            )}
          >
            {meta.icon}
          </div>
          <div className="min-w-0">
            <p className="truncate text-[13px] font-semibold leading-tight text-foreground">
              {step.name}
            </p>
            <div className="mt-0.5 flex items-center gap-1">
              <span className={cn("h-1.5 w-1.5 flex-shrink-0 rounded-full", statusDotClass(step.last_status))} />
              <span className="truncate text-[10px] text-faint">{meta.label}</span>
              <CredsBadge has={hasCreds} />
            </div>
          </div>
        </div>
        <Switch
          checked={step.is_active}
          disabled={toggling}
          onCheckedChange={onToggle}
          aria-label={`Toggle ${step.name}`}
        />
      </div>
    </div>
  );
}

/** Vertical phase column: a title above a stack of step nodes (or an empty
 *  dashed placeholder). */
function PhaseColumn({
  title,
  children,
  empty,
  emptyMsg,
}: {
  title: string;
  children?: React.ReactNode;
  empty: boolean;
  emptyMsg: string;
}) {
  return (
    <div className="flex min-w-[240px] max-w-[300px] flex-shrink-0 flex-col">
      <p className="mb-2.5 text-center text-[10px] font-bold uppercase tracking-wider text-faint">
        {title}
      </p>
      <div className="flex flex-1 flex-col justify-center gap-2.5 rounded-xl px-3 py-3">
        {empty ? (
          <div className="rounded-xl border border-dashed border-line-strong bg-card/60 px-3 py-3 text-center">
            <p className="text-[10px] text-faint">{emptyMsg}</p>
          </div>
        ) : (
          children
        )}
      </div>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────────────
   Header buttons
──────────────────────────────────────────────────────────────────────────── */

function HeaderButton({
  onClick,
  disabled,
  tone,
  icon,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  tone: "ok" | "err" | "warn";
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  const toneCls =
    tone === "ok"
      ? "bg-ok text-white hover:bg-ok/90"
      : tone === "err"
        ? "bg-err text-white hover:bg-err/90"
        : "bg-warn text-white hover:bg-warn/90";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-xs font-bold shadow-sm transition-all",
        toneCls,
        disabled && "cursor-not-allowed opacity-50",
      )}
    >
      {icon}
      {children}
    </button>
  );
}

/* ────────────────────────────────────────────────────────────────────────────
   Main DAG component
──────────────────────────────────────────────────────────────────────────── */

const WORKFLOW_QK = ["workflow-status"];
const INTEGRATIONS_QK = ["integrations"];

export function WorkflowDag({
  data,
  onViewLog,
}: {
  /** The workflow status from the parent's polling query. */
  data: WorkflowStatus;
  /** Open the run-detail dialog (reuses the one in workflow-panel). */
  onViewLog: (runId: number) => void;
}) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: WORKFLOW_QK });

  /* Full integration data for credential awareness (cross-referenced by id). */
  const integrationsQuery = useQuery<IntegrationsData>({
    queryKey: INTEGRATIONS_QK,
    queryFn: api.integrations.getAll,
  });

  const runMut = useMutation({ mutationFn: () => api.workflow.run(), onSuccess: invalidate });
  const killMut = useMutation({
    mutationFn: (id: number) => api.workflow.killRun(id),
    onSuccess: invalidate,
  });
  const toggleMut = useMutation({
    mutationFn: (v: { type: "source" | "destination"; id: number; active: boolean }) =>
      api.workflow.toggleStep(v.type, v.id, v.active),
    onSuccess: invalidate,
  });

  const lastRun = data.runs[0] ?? null;
  const running = isActiveStatus(lastRun?.status);
  const cs = lastRun?.current_stage ?? "";
  const st = lastRun?.status ?? "";
  const hasRun = lastRun !== null;

  /* Credential lookups by id. */
  const { srcCreds, destCreds } = useMemo(() => {
    const fullSources = integrationsQuery.data?.sources ?? [];
    const fullDests = integrationsQuery.data?.destinations ?? [];
    const srcCreds = new Map<number, boolean>();
    for (const s of data.sources) {
      const f = fullSources.find((x) => x.id === s.id);
      if (!f) {
        srcCreds.set(s.id, false);
      } else if (s.source_type === "dbt") {
        srcCreds.set(s.id, !!f.github_repo_url);
      } else {
        srcCreds.set(s.id, f.client_secret_set && !!f.tenant_id && !!f.client_id);
      }
    }
    const destCreds = new Map<number, boolean>();
    for (const d of data.destinations) {
      const f = fullDests.find((x) => x.id === d.id);
      destCreds.set(d.id, f ? f.bq_service_account_set : false);
    }
    return { srcCreds, destCreds };
  }, [integrationsQuery.data, data.sources, data.destinations]);

  const transformationSources = data.sources.filter((s) => s.category === "transformation");
  const visualizationSources = data.sources.filter((s) => s.category !== "transformation");

  function renderSourceNode(s: WorkflowStepSummary) {
    return (
      <StepNode
        key={s.id}
        step={s}
        kind="source"
        meta={sourceMeta(s.source_type)}
        hasCreds={srcCreds.get(s.id) ?? false}
        highlighted={running && cs === "sources" && s.is_active}
        toggling={toggleMut.isPending}
        onToggle={(checked) => toggleMut.mutate({ type: "source", id: s.id, active: checked })}
      />
    );
  }

  return (
    <div
      className={cn(
        "overflow-hidden rounded-2xl border shadow-card",
        running ? "border-run/40 bg-run/[0.03] ring-1 ring-run/20" : "border-line bg-card",
      )}
    >
      {/* Dark gradient header */}
      <div className="flex items-center justify-between bg-gradient-to-r from-welcome-blue via-foreground to-welcome-blue px-6 py-3.5">
        <h3 className="flex items-center text-sm font-semibold tracking-wide text-white">
          <Zap className="mr-2 h-4 w-4 text-ok" />
          ETL Pipeline
          {running && (
            <span className="ml-3 inline-flex items-center rounded-full border border-run/40 bg-run/20 px-2 py-0.5 text-[10px] font-semibold text-white">
              <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-run" />
              {STAGE_LABEL[cs] || cs || "Running"}
            </span>
          )}
        </h3>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={invalidate}
            className="rounded p-1 text-white/60 transition-colors hover:text-white"
            title="Refresh"
            aria-label="Refresh pipeline"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
          <HeaderButton
            onClick={() => runMut.mutate()}
            disabled={running || runMut.isPending}
            tone="ok"
            icon={runMut.isPending ? <Spinner className="h-3.5 w-3.5 text-white" /> : <Play className="h-3.5 w-3.5" />}
          >
            Run Pipeline
          </HeaderButton>
          {running && lastRun && (
            <HeaderButton
              onClick={() => killMut.mutate(lastRun.id)}
              disabled={killMut.isPending}
              tone="err"
              icon={<Square className="h-3.5 w-3.5" />}
            >
              Stop Pipeline
            </HeaderButton>
          )}
          {!running && st === "failed" && (
            <HeaderButton
              onClick={() => runMut.mutate()}
              disabled={runMut.isPending}
              tone="warn"
              icon={<RotateCw className="h-3.5 w-3.5" />}
            >
              Retry
            </HeaderButton>
          )}
        </div>
      </div>

      {/* DAG graph — horizontal flow, vertical source/dest stacking. */}
      <div className="overflow-x-auto bg-[radial-gradient(circle_at_top,_oklch(var(--panel-2))_0,_oklch(var(--card))_58%)] px-5 py-7">
        <div className="flex min-w-[820px] items-stretch justify-center gap-3">
          <StageBox label="Prepare" state={stageState("init", cs, st, hasRun)} />
          <ArrowCol />

          <PhaseColumn
            title="Transformation"
            empty={transformationSources.length === 0}
            emptyMsg="No transformation sources"
          >
            {transformationSources.map(renderSourceNode)}
          </PhaseColumn>
          <ArrowCol />
          <PhaseColumn
            title="Visualization"
            empty={visualizationSources.length === 0}
            emptyMsg="No visualization sources"
          >
            {visualizationSources.map(renderSourceNode)}
          </PhaseColumn>

          <ArrowCol />
          <StageBox label="Finish" state={stageState("final", cs, st, hasRun)} />
          <ArrowCol />

          <PhaseColumn
            title="Destinations"
            empty={data.destinations.length === 0}
            emptyMsg="No destinations configured"
          >
            {data.destinations.map((d) => (
              <StepNode
                key={d.id}
                step={d}
                kind="destination"
                meta={destMeta(d.destination_type)}
                hasCreds={destCreds.get(d.id) ?? false}
                highlighted={running && cs === "destinations" && d.is_active}
                toggling={toggleMut.isPending}
                onToggle={(checked) =>
                  toggleMut.mutate({ type: "destination", id: d.id, active: checked })
                }
              />
            ))}
          </PhaseColumn>
        </div>

        {/* Legend */}
        <div className="mt-4 flex items-center justify-center gap-4 border-t border-line pt-3 text-[10px] text-faint">
          <span className="flex items-center gap-1">
            <span className="h-1.5 w-1.5 rounded-full bg-ok" />
            Success
          </span>
          <span className="flex items-center gap-1">
            <span className="h-1.5 w-1.5 rounded-full bg-err" />
            Failed
          </span>
          <span className="flex items-center gap-1">
            <span className="h-1.5 w-1.5 rounded-full bg-faint/40" />
            Idle
          </span>
          <span className="flex items-center gap-1">
            <span className="font-bold text-ok">✓</span>
            Credentials
          </span>
          <span className="flex items-center gap-1">
            <span className="text-warn">⚠</span>
            No credentials
          </span>
        </div>
      </div>

      {runMut.isError && (
        <div className="border-t border-line bg-err/[0.06] px-6 py-2 text-[12px] text-err">
          Could not start the workflow.
        </div>
      )}

      {/* Last-run bar */}
      {lastRun ? (
        <div className="flex items-center justify-between border-t border-line bg-panel2 px-6 py-2.5 text-xs text-muted-foreground">
          <span className="flex flex-wrap items-center gap-2">
            <span>Last: {fmtWhen(lastRun.started_at)}</span>
            <span className="text-faint">·</span>
            <StatusBadge status={lastRun.status} />
            {lastRun.duration_seconds != null && (
              <>
                <span className="text-faint">·</span>
                <span>{lastRun.duration_seconds}s</span>
              </>
            )}
          </span>
          <button
            type="button"
            onClick={() => onViewLog(lastRun.id)}
            className="inline-flex items-center gap-1.5 font-medium text-brand hover:text-welcome-tealhover"
          >
            <ScrollText className="h-3.5 w-3.5" /> View Log
          </button>
        </div>
      ) : (
        <div className="border-t border-line bg-panel2 px-6 py-2.5 text-center">
          <p className="text-[11px] text-faint">
            No workflow runs yet. Click &quot;Run Pipeline&quot; to start.
          </p>
        </div>
      )}
    </div>
  );
}
