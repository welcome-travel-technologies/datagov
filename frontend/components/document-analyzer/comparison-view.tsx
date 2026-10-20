"use client";

import { useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ChevronDown, Scale } from "lucide-react";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { getApiErrorMessage } from "@/lib/api";
import { fmtWhen } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  analyzerApi,
  isComparisonHidden,
  withoutImages,
  type ComparisonArmSummary,
  type ComparisonRow,
  type ComparisonRunInfo,
  type ComparisonSummary,
} from "@/lib/document-analyzer-api";
import {
  answerStyle,
  armLabel,
  armShortLabel,
  chanceText,
  ciText,
  configDrift,
  DASH,
  differs,
  disagreementSentence,
  disagreesTooMuch,
  docsMissing,
  excludedQuestions,
  filterArms,
  filterRows,
  findVerdict,
  flipsText,
  fmtArmCost,
  fmtCount,
  fmtOf,
  fmtPct,
  fmtQuestionDelta,
  fmtSeconds,
  fmtSpent,
  goldenSetLine,
  infraErrors,
  isDryRun,
  isRunActive,
  kindCell,
  kindLabel,
  noiseSentence,
  orderArms,
  paramText,
  partialRunText,
  plural,
  QUESTION_FILTERS,
  RECORDED,
  RECORDED_NOISE,
  recordedArm,
  recordedScoreSentence,
  recordedSentence,
  rowDocsMissing,
  rowVerdict,
  runClass,
  runLabel,
  TONE_STYLES,
  verdictText,
  verdictTone,
  withRecorded,
  type QuestionFilter,
} from "./comparison-format";

const selectClass = "h-9 w-full rounded-lg border border-input bg-panel px-3 text-[13px] text-foreground outline-none focus:ring-1 focus:ring-ring";
const sectionTitle = "text-[14px] font-semibold";
const chip = "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium";

/** How DataGov's answers compare with Luccy's on the same questions, one run
 *  at a time (org admins). `runs` is the list, newest first. */
export function ComparisonView({ runs, pending, error, onRetry }: {
  runs?: ComparisonRunInfo[];
  pending: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const [picked, setPicked] = useState<string | null>(null);
  const list = runs ?? [];
  const listed = list.find((r) => r.run_id === picked) ?? list[0] ?? null;
  const runId = listed?.run_id ?? null;
  const detail = useQuery({
    queryKey: ["document-analyzer", "comparison", runId],
    queryFn: () => analyzerApi.comparison(runId as string),
    enabled: !!runId,
    retry: false,
    // Finished, failed and stale runs no longer change: asked once.
    refetchInterval: (query) => (isRunActive(query.state.data) ? 10_000 : false),
  });

  let content: React.ReactNode;
  if (pending) content = <LoadingState label="Loading comparison runs…" />;
  else if (error && !runs) {
    content = <Problem text={getApiErrorMessage(error, "The comparison runs could not be loaded.")} onRetry={onRetry} />;
  } else if (!listed) {
    content = (
      <Card><EmptyState icon={<Scale className="h-8 w-8" />} title="No comparison runs yet"
        hint="A run appears here once a comparison has been started on the server." /></Card>
    );
  } else {
    // The list already carries the summary, so the numbers show while the answers load.
    const run = detail.data && detail.data.run_id === runId ? detail.data : listed;
    content = (
      <>
        <RunPicker runs={list} run={run} onPick={setPicked} />
        {isDryRun(run) && <DryRunBanner />}
        <PartialRunBanner run={run} />
        <GoldenSet summary={run.summary ?? null} />
        <RunReport summary={run.summary ?? null} />
        {detail.isPending ? (
          <LoadingState label="Loading the answers…" />
        ) : detail.isError && !detail.data ? (
          <Problem
            text={isComparisonHidden(detail.error) ? "This run isn't available any more." : getApiErrorMessage(detail.error, "The answers of this run could not be loaded.")}
            onRetry={() => { void detail.refetch(); }}
          />
        ) : (
          <QuestionList rows={detail.data?.rows ?? []} summary={run.summary ?? null} />
        )}
        <Notes summary={run.summary ?? null} rows={detail.data?.rows ?? []} />
      </>
    );
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto pb-6">
      <div className="mx-auto max-w-6xl space-y-5">{content}</div>
    </div>
  );
}

function Problem({ text, onRetry }: { text: string; onRetry: () => void }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <EmptyState title="Couldn't load the comparison" hint={text} />
        <div className="-mt-6 flex justify-center pb-2"><Button size="sm" variant="outline" onClick={onRetry}>Try again</Button></div>
      </CardContent>
    </Card>
  );
}

function runOption(run: ComparisonRunInfo): string {
  const when = fmtWhen(run.created_at);
  return `${when === DASH ? run.run_id : when} · ${runLabel(run)}${isDryRun(run) ? " · dry run" : ""}`;
}

/** A dry run's answers are made up: its numbers must not pass for a result. */
function DryRunBanner() {
  return (
    <div role="note" aria-label="Dry run" className="flex items-start gap-3 rounded-xl border-2 border-warn bg-warn/15 p-4">
      <AlertTriangle aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-warn" />
      <div>
        <p className="text-[16px] font-semibold text-warn">Dry run — not real answers</p>
        <p className="mt-1 text-[13px] text-foreground/80">
          This run tested the comparison itself with made-up answers and a stand-in judge, at no cost. Its numbers say nothing about how DataGov or Luccy answer.
        </p>
      </div>
    </div>
  );
}

/** A smoke run (part of the golden set) or a run stopped early: its numbers
 *  can't be set against Luccy's recorded score. */
function PartialRunBanner({ run }: { run: ComparisonRunInfo }) {
  const text = partialRunText(run);
  if (!text) return null;
  return (
    <div role="note" aria-label="Partial run" className="flex items-start gap-3 rounded-xl border-2 border-warn bg-warn/15 p-4">
      <AlertTriangle aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-warn" />
      <div>
        <p className="text-[16px] font-semibold text-warn">Partial run — not the full golden set</p>
        <p className="mt-1 text-[13px] text-foreground/80">{text}</p>
      </div>
    </div>
  );
}

/** Which questions were asked, Luccy's recorded score on them, and whether
 *  Luccy's configuration is still the one that score was recorded with.
 *  Older runs carry none of it and show nothing here. */
function GoldenSet({ summary }: { summary: ComparisonSummary | null }) {
  const line = goldenSetLine(summary);
  const recorded = recordedSentence(summary);
  const drift = configDrift(summary);
  if (!line && !recorded && !drift) return null;
  return (
    <section aria-labelledby="comparison-golden" className="space-y-2">
      <h2 id="comparison-golden" className={sectionTitle}>Golden set</h2>
      <div className="space-y-2 rounded-lg border border-line bg-card p-4 text-[13px]">
        {line && (
          <p className={cn("flex items-start gap-1.5", line.same ? "text-foreground" : "font-medium text-warn")}>
            {line.same
              ? <CheckCircle2 aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-ok" />
              : <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />}
            <span>{line.text}</span>
          </p>
        )}
        {recorded && <p>{recorded}</p>}
        {recorded && <p className="text-[12.5px] text-muted-foreground">{RECORDED_NOISE}</p>}
        {drift && (
          <div>
            {drift.same ? (
              <p>Luccy&apos;s configuration is the one its score was recorded with (retrieval settings, prompts and models).</p>
            ) : (
              <>
                <p className="font-medium text-warn">Luccy&apos;s configuration changed since its score was recorded:</p>
                <ul className="mt-1 list-disc space-y-0.5 pl-5">
                  {drift.lines.map((text, i) => <li key={`${i}-${text}`} className="break-words">{text}</li>)}
                </ul>
              </>
            )}
            <p className="mt-1 text-muted-foreground">{drift.documents}</p>
          </div>
        )}
      </div>
    </section>
  );
}

function RunPicker({ runs, run, onPick }: { runs: ComparisonRunInfo[]; run: ComparisonRunInfo; onPick: (id: string) => void }) {
  const id = useId();
  const summary = run.summary;
  const params = Object.entries(run.params ?? {});
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-end gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1 basis-60 space-y-1">
          <label htmlFor={id} className="block text-[12px] font-medium text-muted-foreground">Comparison run</label>
          <select id={id} value={run.run_id} onChange={(event) => onPick(event.target.value)} className={selectClass}>
            {runs.map((r) => <option key={r.run_id} value={r.run_id}>{runOption(r)}</option>)}
          </select>
        </div>
        <span className={cn(chip, "h-6 px-2.5", runClass(run))}>{runLabel(run)}</span>
      </div>
      <p className="mt-2 text-[12px] text-muted-foreground">
        {typeof summary?.questions === "number" ? plural(summary.questions, "question") : `${DASH} questions`}
        {" · "}started {fmtWhen(run.created_at)} · updated {fmtWhen(run.updated_at)} · spent {fmtSpent(summary)}
      </p>
      {params.length > 0 && (
        <details className="mt-2 text-[12px]">
          <summary className="cursor-pointer text-muted-foreground">Run settings</summary>
          <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-[auto_1fr]">
            {params.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="font-medium text-muted-foreground">{key}</dt>
                <dd className="mb-1 break-words sm:mb-0">{paramText(value)}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </Card>
  );
}

/** The verdict (DataGov chat against Luccy), the headline numbers, agreement
 *  and the per-kind table. */
function RunReport({ summary }: { summary: ComparisonSummary | null }) {
  const arms = orderArms(summary);
  return (
    <>
      <MainVerdict summary={summary} />
      <Headline summary={summary} arms={arms} />
      <Agreement summary={summary} />
      <ByKind summary={summary} arms={arms} />
    </>
  );
}

function ToneBadge({ tone }: { tone: keyof typeof TONE_STYLES }) {
  const style = TONE_STYLES[tone];
  return <span className={cn(chip, "border border-current bg-card", style.text)}>{style.badge}</span>;
}

/** The runner found this arm disagreeing with Luccy more than Luccy does with itself. */
function DisagreeBadge() {
  return (
    <span className={cn(chip, "gap-1 border border-warn/50 bg-warn/15 text-warn")}
      title="Its right and wrong answers differ from Luccy's on more questions than two runs of Luccy differ from each other.">
      <AlertTriangle aria-hidden="true" className="h-3 w-3" />Disagrees with Luccy more than Luccy does with itself
    </span>
  );
}

function MainVerdict({ summary }: { summary: ComparisonSummary | null }) {
  const verdict = findVerdict(summary, "C", "A");
  const tone = verdictTone(verdict);
  const style = TONE_STYLES[tone];
  const c = summary?.arms?.C, a = summary?.arms?.A;
  const ci = ciText(verdict);
  const recordedScore = recordedScoreSentence(summary);
  return (
    <section aria-label="Verdict: DataGov chat compared with Luccy" className={cn("rounded-xl border p-4 sm:p-5", style.card)}>
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">{armLabel("C", summary)} compared with {armLabel("A", summary)}</h2>
        <ToneBadge tone={tone} />
        {disagreesTooMuch(verdict) && <DisagreeBadge />}
      </div>
      <p className={cn("mt-1.5 text-[18px] font-semibold leading-snug sm:text-[20px]", style.text)}>{verdictText(verdict, summary)}</p>
      <p className="mt-2 text-[13px] text-foreground/80">
        Right answers: {armShortLabel("C", summary)} {fmtCount(c?.pass)} ({fmtPct(c?.pass_rate)}) · {armShortLabel("A", summary)} {fmtCount(a?.pass)} ({fmtPct(a?.pass_rate)}) · difference {fmtQuestionDelta(verdict?.delta)}{ci && ` (${ci})`}
      </p>
      {verdict && <p className="mt-1 text-[13px] text-foreground/80">{disagreementSentence(verdict, summary)}</p>}
      {recordedScore && <p className="mt-1 text-[13px] text-foreground/80">{recordedScore}</p>}
      <p className="mt-1 text-[12.5px] text-muted-foreground">{noiseSentence(summary)}</p>
    </section>
  );
}

type Measure = { label: string; hint?: string; value: (s: ComparisonArmSummary) => string; bad?: (s: ComparisonArmSummary) => boolean };

const MEASURES: Measure[] = [
  { label: "Questions answered", value: (s) => fmtCount(s.answered) },
  { label: "Right answers", hint: "Share of the questions marked right", value: (s) => fmtPct(s.pass_rate) },
  { label: "Expected document found", hint: "Share of the questions where the document that holds the answer was found", value: (s) => fmtPct(s.expected_doc_hit_rate) },
  { label: "Facts covered", hint: "Share of the expected facts that the answers contain", value: (s) => fmtPct(s.fact_recall) },
  { label: "Correct “I don't know”", hint: "Questions the documents can't answer, where it rightly said so", value: (s) => fmtOf(s.correct_abstentions, s.abstain_rows) },
  { label: "Privacy leaks", hint: "Answers that showed an email address, phone number or similar", value: (s) => fmtCount(s.privacy_leaks), bad: (s) => (s.privacy_leaks ?? 0) > 0 },
  { label: "Errors", value: (s) => fmtCount(s.errors), bad: (s) => (s.errors ?? 0) > 0 },
  { label: "Infrastructure errors", hint: "Questions it couldn't answer because a service failed (a timeout, a refused connection); left out of its scores", value: (s) => fmtCount(s.infra_errors), bad: (s) => (s.infra_errors ?? 0) > 0 },
  { label: "Typical time (median)", hint: "Half the answers were faster than this", value: (s) => fmtSeconds(s.p50_latency_s) },
  { label: "Slow answers (90th percentile)", hint: "9 in 10 answers were faster than this", value: (s) => fmtSeconds(s.p90_latency_s) },
  { label: "Cost", hint: "Marked “estimated” where the run could not measure what was actually charged", value: (s) => fmtArmCost(s) },
];

const stickyCell = "sticky left-0 z-[1] bg-card";
const headCell = "px-3 py-2 text-left align-bottom text-[11px] font-semibold text-muted-foreground";

function Headline({ summary, arms }: { summary: ComparisonSummary | null; arms: string[] }) {
  const columns = withRecorded(arms, summary);
  const figures = (arm: string): ComparisonArmSummary => (arm === RECORDED ? recordedArm(summary) : summary?.arms?.[arm]) ?? {};
  return (
    <section aria-labelledby="comparison-headline" className="space-y-2">
      <h2 id="comparison-headline" className={sectionTitle}>Headline numbers</h2>
      {arms.length === 0 ? <p className="text-[13px] text-muted-foreground">No numbers yet.</p> : (
        <div className="overflow-x-auto rounded-lg border border-line bg-card">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="border-b border-line">
                <th scope="col" className={cn(headCell, stickyCell, "min-w-[9.5rem]")}>Measure</th>
                {columns.map((arm) => (
                  <th key={arm} scope="col" className={cn(headCell, "min-w-[6.5rem] text-right", arm === RECORDED && "italic")}
                    title={arm === RECORDED ? "Luccy's recorded score on the same questions: a reference, not asked in this run" : undefined}>
                    {armLabel(arm, summary)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {MEASURES.map((m) => (
                <tr key={m.label} className="border-t border-line first:border-t-0">
                  <th scope="row" className={cn(stickyCell, "px-3 py-2 text-left font-medium")} title={m.hint}>{m.label}</th>
                  {columns.map((arm) => {
                    const s = figures(arm);
                    return <td key={arm} className={cn("px-3 py-2 text-right tabular-nums", m.bad?.(s) && "font-semibold text-err")}>{m.value(s)}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Agreement({ summary }: { summary: ComparisonSummary | null }) {
  const pairs = (summary?.agreement ?? []).filter((p) => p && p.a && p.b);
  return (
    <section aria-labelledby="comparison-agreement" className="space-y-2">
      <h2 id="comparison-agreement" className={sectionTitle}>Where they agree</h2>
      <p className="text-[12.5px] text-muted-foreground">For each pair, how many questions both got right, only one got right, or both got wrong.</p>
      {pairs.length === 0 ? <p className="text-[13px] text-muted-foreground">{DASH}</p> : (
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {pairs.map((p, i) => {
            const chance = chanceText(p.mcnemar_p);
            const flips = flipsText(p);
            const cells: [string, unknown, string][] = [
              ["Both right", p.both_pass, "text-ok"],
              [`Only ${armShortLabel(p.a, summary)} right`, p.only_a, ""],
              [`Only ${armShortLabel(p.b, summary)} right`, p.only_b, ""],
              ["Both wrong", p.both_fail, "text-err"],
            ];
            return (
              <li key={`${p.a}-${p.b}-${i}`} className="rounded-lg border border-line bg-card p-3">
                <h3 className="text-[13px] font-medium">{armLabel(p.a, summary)} and {armLabel(p.b, summary)}</h3>
                <dl className="mt-2 grid grid-cols-2 gap-2">
                  {cells.map(([label, value, color]) => (
                    <div key={label} className="rounded-md bg-panel2 px-2.5 py-1.5">
                      <dt className="text-[11.5px] text-muted-foreground">{label}</dt>
                      <dd className={cn("text-[16px] font-semibold tabular-nums", color)}>{fmtCount(value)}</dd>
                    </div>
                  ))}
                </dl>
                {flips && <p className="mt-2 text-[11.5px] text-muted-foreground">{flips}</p>}
                {chance && <p className="mt-2 text-[11.5px] text-muted-foreground">{chance}</p>}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function ByKind({ summary, arms }: { summary: ComparisonSummary | null; arms: string[] }) {
  const kinds = Object.keys(summary?.by_kind ?? {}).sort((x, y) => x.localeCompare(y));
  const columns = kinds.some((kind) => summary?.by_kind?.[kind]?.[RECORDED]) ? [...arms, RECORDED] : arms;
  return (
    <section aria-labelledby="comparison-kinds" className="space-y-2">
      <h2 id="comparison-kinds" className={sectionTitle}>Right answers by kind of question</h2>
      {kinds.length === 0 || arms.length === 0 ? <p className="text-[13px] text-muted-foreground">{DASH}</p> : (
        <div className="overflow-x-auto rounded-lg border border-line bg-card">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="border-b border-line">
                <th scope="col" className={cn(headCell, stickyCell, "min-w-[8rem]")}>Kind</th>
                {columns.map((arm) => <th key={arm} scope="col" className={cn(headCell, "min-w-[6.5rem] text-right", arm === RECORDED && "italic")}>{armLabel(arm, summary)}</th>)}
              </tr>
            </thead>
            <tbody>
              {kinds.map((kind) => (
                <tr key={kind} className="border-t border-line first:border-t-0">
                  <th scope="row" className={cn(stickyCell, "px-3 py-2 text-left font-medium")}>{kindLabel(kind)}</th>
                  {columns.map((arm) => {
                    const cell = kindCell(summary?.by_kind?.[kind]?.[arm]);
                    return (
                      <td key={arm} className="px-3 py-2 text-right tabular-nums">
                        <span className="block">{cell.main}</span>
                        <span className="block text-[11px] text-faint">{cell.pct}</span>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function QuestionList({ rows, summary }: { rows: ComparisonRow[]; summary: ComparisonSummary | null }) {
  const [filter, setFilter] = useState<QuestionFilter>("all");
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());
  const arms = useMemo(() => orderArms(summary, rows), [summary, rows]);
  const available = useMemo(() => filterArms(arms, rows), [arms, rows]);
  const filters = QUESTION_FILTERS.filter((f) => !f.arms || f.arms.every((arm) => available.includes(arm)));
  const active = filters.some((f) => f.id === filter) ? filter : "all";
  const shown = filterRows(rows, active);
  const toggle = (id: string) => setOpen((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return (
    <section aria-labelledby="comparison-questions" className="space-y-2">
      <h2 id="comparison-questions" className={sectionTitle}>Question by question</h2>
      {filters.length > 1 && (
        <div role="group" aria-label="Show questions" className="flex flex-wrap gap-1.5">
          {filters.map((f) => {
            const count = f.arms ? rows.filter((row) => differs(row, f.arms![0], f.arms![1])).length : rows.length;
            return (
              <button
                key={f.id}
                type="button"
                aria-pressed={active === f.id}
                onClick={() => setFilter(f.id)}
                className={cn(
                  "h-7 rounded-full border px-2.5 text-[12px] font-medium transition-colors",
                  active === f.id ? "border-brand/40 bg-brand/10 text-foreground" : "border-line text-muted-foreground hover:text-foreground",
                )}
              >
                {f.label} ({count})
              </button>
            );
          })}
        </div>
      )}
      {rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No answers yet.</p>
      ) : shown.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No questions differ here.</p>
      ) : (
        <ul className="space-y-2">
          {shown.map((row) => <QuestionItem key={row.id} row={row} arms={arms} summary={summary} open={open.has(row.id)} onToggle={() => toggle(row.id)} />)}
        </ul>
      )}
    </section>
  );
}

function QuestionItem({ row, arms, summary, open, onToggle }: {
  row: ComparisonRow; arms: string[]; summary: ComparisonSummary | null; open: boolean; onToggle: () => void;
}) {
  const panelId = `${useId()}-answers`;
  const present = arms.filter((arm) => row.arms?.[arm]);
  const recorded = rowVerdict(row, RECORDED);
  const missing = rowDocsMissing(row);
  return (
    <li className="rounded-lg border border-line bg-card">
      <button type="button" aria-expanded={open} aria-controls={open ? panelId : undefined} onClick={onToggle}
        className="flex w-full items-start gap-2 p-3 text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
        <ChevronDown aria-hidden="true" className={cn("mt-0.5 h-4 w-4 shrink-0 text-faint transition-transform", !open && "-rotate-90")} />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-1.5 text-[11px] text-faint">
            <span className="font-mono">{row.id}</span>
            {row.kind && <span>· {kindLabel(row.kind)}</span>}
          </span>
          <span className="mt-0.5 block break-words text-[13px] font-medium">{row.question || DASH}</span>
          <span className="mt-2 flex flex-wrap gap-1.5">
            {present.map((arm) => {
              const style = answerStyle(row.arms?.[arm]?.verdict);
              return <span key={arm} className={cn(chip, style.className)}>{armShortLabel(arm, summary)}: {style.label}</span>;
            })}
            {recorded !== undefined && (
              <span className={cn(chip, "border border-dashed border-line bg-transparent text-muted-foreground")}
                title={`Luccy's recorded verdict on this question (${armLabel(RECORDED, summary)})`}>
                {armShortLabel(RECORDED, summary)}: {answerStyle(recorded).label}
              </span>
            )}
            {missing.length > 0 && (
              <span className={cn(chip, "gap-1 bg-warn/15 text-warn")} title={`Not in DataGov: ${missing.join(", ")}`}>
                <AlertTriangle aria-hidden="true" className="h-3 w-3" />Expected document not in DataGov
              </span>
            )}
          </span>
        </span>
      </button>
      {open && (
        <div id={panelId} className="grid gap-3 border-t border-line p-3 md:grid-cols-2 2xl:grid-cols-3">
          {missing.length > 0 && (
            <p className="text-[12px] text-muted-foreground md:col-span-2 2xl:col-span-3">
              Expected documents not in DataGov: <span className="font-mono">{missing.join(", ")}</span>. The question is scored anyway.
            </p>
          )}
          {present.length === 0 ? <p className="text-[13px] text-muted-foreground">No answers.</p> : present.map((arm) => {
            const leg = row.arms?.[arm] ?? {};
            const style = answerStyle(leg.verdict);
            const cited = Array.isArray(leg.cited) ? leg.cited.filter((c) => typeof c === "string") : [];
            return (
              <article key={arm} className="min-w-0 rounded-lg border border-line bg-panel p-3">
                <header className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-[12.5px] font-semibold">{armLabel(arm, summary)}</h3>
                  <span className={cn(chip, style.className)}>{style.label}</span>
                </header>
                <p className="mt-1 text-[11.5px] text-muted-foreground">{fmtSeconds(leg.latency_s)} · facts covered {fmtPct(leg.fact_ratio)}</p>
                <div className="mt-2 max-h-80 overflow-y-auto break-words [overflow-wrap:anywhere]">
                  {typeof leg.answer === "string" && leg.answer.trim()
                    ? <Markdown>{withoutImages(leg.answer)}</Markdown>
                    : <p className="text-[13px] text-muted-foreground">No answer.</p>}
                </div>
                <p className="mt-2 break-all text-[11px] text-faint">Cited: {cited.length ? cited.join(", ") : DASH}</p>
              </article>
            );
          })}
        </div>
      )}
    </li>
  );
}

/** What to keep in mind when reading the scores: answers lost to a service
 *  failure (the only thing left out of them) and questions whose expected
 *  documents DataGov does not hold (scored anyway). Older runs left more out:
 *  their lists show as they were. */
function Notes({ summary, rows }: { summary: ComparisonSummary | null; rows: ComparisonRow[] }) {
  const excluded = summary?.excluded;
  const failures = infraErrors(excluded?.infra_error);
  const missing = docsMissing(summary?.expected_docs_missing);
  const older: [string, ReturnType<typeof excludedQuestions>][] = ([
    ["Need live warehouse data", excludedQuestions(excluded?.warehouse, rows)],
    ["The documents they need aren't in the copy being searched", excludedQuestions(excluded?.not_in_corpus, rows)],
  ] as [string, ReturnType<typeof excludedQuestions>][]).filter(([, items]) => items !== null);
  if (failures === null && missing === null && older.length === 0) return null;
  const idCell = (id: string) => <span className="font-mono text-[12px] text-foreground/80">{id}</span>;
  return (
    <section aria-labelledby="comparison-notes" className="space-y-2">
      <h2 id="comparison-notes" className={sectionTitle}>Service failures and missing documents</h2>
      <div className="space-y-3 rounded-lg border border-line bg-card p-4 text-[13px]">
        {/* Older runs have no such lists: each group shows once a run reports it. */}
        {failures !== null && (
          <div>
            <h3 className="font-medium">Answers lost to a service failure ({failures.length})</h3>
            <p className="text-[12px] text-muted-foreground">The only answers left out of the scores.</p>
            {failures.length === 0 ? <p className="text-muted-foreground">None</p> : (
              <ul className="mt-1 space-y-0.5 text-muted-foreground">
                {failures.map((f, i) => (
                  <li key={`${f.id}-${f.arm}-${i}`} className="break-words">
                    {idCell(f.id)} · <span className="font-medium text-foreground/80">{f.arm ? armLabel(f.arm, summary) : DASH}</span>: {f.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {missing !== null && (
          <div>
            <h3 className="font-medium">Expected documents not in DataGov ({missing.length})</h3>
            <p className="text-[12px] text-muted-foreground">Asked and scored anyway: a wrong answer here may only mean DataGov doesn&apos;t hold the document.</p>
            {missing.length === 0 ? <p className="text-muted-foreground">None</p> : (
              <ul className="mt-1 space-y-0.5 text-muted-foreground">
                {missing.map((m, i) => (
                  <li key={`${m.id}-${i}`} className="break-words">
                    {idCell(m.id)}{m.question ? ` — ${m.question}` : ""}{m.missing.length > 0 && <> · missing <span className="font-mono text-[12px]">{m.missing.join(", ")}</span></>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {older.length > 0 && <p className="text-[12px] text-muted-foreground">This older run also left these questions out of its scores:</p>}
        {older.map(([label, items]) => (
          <div key={label}>
            <h3 className="font-medium">{label} ({items ? items.length : DASH})</h3>
            {!items || items.length === 0 ? <p className="text-muted-foreground">None</p> : (
              <ul className="mt-1 space-y-0.5 text-muted-foreground">
                {items.map((item, i) => <li key={`${item.id}-${i}`} className="break-words">{idCell(item.id)}{item.question ? ` — ${item.question}` : ""}</li>)}
              </ul>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
