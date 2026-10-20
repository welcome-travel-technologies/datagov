/**
 * Pure helpers for the comparison view: plain-words formatting of the run's
 * numbers, arm names and order, verdict styling, the golden set and Luccy's
 * recorded run, and the question filters. Anything missing or not a number
 * shows as "—".
 */
import type {
  ComparisonAgreement,
  ComparisonArmSummary,
  ComparisonRow,
  ComparisonRunInfo,
  ComparisonSummary,
  ComparisonVerdict,
} from "@/lib/document-analyzer-api";

export const DASH = "—";

/** The arms in the order the page shows them: Luccy (A), Luccy again (A2,
 *  its noise floor) and DataGov chat with only the analyzer (C). Unknown arms
 *  follow in their own order, under the labels their run gave them. */
export const ARM_ORDER = ["A", "A2", "C"] as const;

/** Luccy's recorded run on the golden set: a reference column next to the
 *  arms (agreement, per kind, per question), never an arm that was asked. */
export const RECORDED = "recorded";

export const ARM_LABELS: Record<string, string> = {
  A: "Luccy",
  A2: "Luccy (2nd run)",
  C: "DataGov chat · Luccy only",
  [RECORDED]: "Luccy, recorded",
};

/** Short names for the per-question chips, where space is tight. */
const ARM_SHORT: Record<string, string> = { A: "Luccy", A2: "Luccy 2nd", C: "Chat", [RECORDED]: "Recorded" };

const isNum = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** Up to one decimal, without a trailing ".0". */
function oneDecimal(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return (Object.is(rounded, -0) ? 0 : rounded).toLocaleString("en-US", { maximumFractionDigits: 1 });
}

/** A rate in percent. Every *_rate and fact_recall is a fraction: 0.725 → "72.5%". */
export function fmtPct(value: unknown): string {
  if (!isNum(value)) return DASH;
  return `${oneDecimal(value * 100)}%`;
}

/** A number of questions: "1 question", "6 questions". Verdict deltas,
 *  thresholds and the noise floor are whole question counts. */
export function fmtQuestions(value: unknown): string {
  return isNum(value) ? plural(Math.abs(Math.round(value)), "question") : DASH;
}

/** A difference in questions with its sign: "+3 questions", "−1 question", "0 questions". */
export function fmtQuestionDelta(value: unknown): string {
  if (!isNum(value)) return DASH;
  const n = Math.round(value);
  return n === 0 ? fmtQuestions(0) : `${n > 0 ? "+" : "−"}${fmtQuestions(n)}`;
}

/** Seconds as people say them: "0.8 s", "12 s", "2 min 5 s". */
export function fmtSeconds(value: unknown): string {
  if (!isNum(value) || value < 0) return DASH;
  if (value < 10) return `${oneDecimal(value)} s`;
  if (value < 60) return `${Math.round(value)} s`;
  const total = Math.round(value);
  const minutes = Math.floor(total / 60), seconds = total % 60;
  return seconds ? `${minutes} min ${seconds} s` : `${minutes} min`;
}

/** Dollars: cents for amounts from 1¢ up, four decimals below that. */
export function fmtUsd(value: unknown): string {
  if (!isNum(value) || value < 0) return DASH;
  if (value === 0) return "$0";
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** A difference of two rates in percentage points, signed: 0.05 → "+5", −0.125 → "−12.5". */
export function fmtPoints(value: unknown): string {
  if (!isNum(value)) return DASH;
  const shown = oneDecimal(value * 100);
  return shown === "0" ? "0" : value > 0 ? `+${shown}` : `−${shown.replace(/^-/, "")}`;
}

/** One arm's cost, marked "estimated" unless the run measured it. */
export function fmtArmCost(arm?: Pick<ComparisonArmSummary, "est_cost_usd" | "cost_source"> | null): string {
  const shown = fmtUsd(arm?.est_cost_usd);
  return shown === DASH || arm?.cost_source === "measured" ? shown : `${shown} (estimated)`;
}

/** The run's spend, "estimated" unless every arm's cost was measured. */
export function fmtSpent(summary?: ComparisonSummary | null): string {
  const shown = fmtUsd(summary?.spent_usd);
  const arms = Object.values(summary?.arms ?? {});
  const measured = arms.length > 0 && arms.every((arm) => arm?.cost_source === "measured");
  return shown === DASH || measured ? shown : `${shown} (estimated)`;
}

export function fmtCount(value: unknown): string {
  return isNum(value) ? value.toLocaleString("en-US") : DASH;
}

/** "4 of 5"; "—" when either side is missing. */
export function fmtOf(part: unknown, whole: unknown): string {
  return isNum(part) && isNum(whole) ? `${fmtCount(part)} of ${fmtCount(whole)}` : DASH;
}

export function plural(count: number, word: string): string {
  return `${count.toLocaleString("en-US")} ${word}${count === 1 ? "" : "s"}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A day as "28 Sep" (or "28 Sep 2026"), in UTC and the same in every locale; "" without a valid date. */
export function fmtDay(iso: unknown, withYear = false): string {
  if (typeof iso !== "string" || !iso.trim()) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]}${withYear ? ` ${date.getUTCFullYear()}` : ""}`;
}

// ---- arms ---------------------------------------------------------------------

export function armLabel(arm: string, summary?: ComparisonSummary | null): string {
  if (arm === RECORDED) return recordedLabel(summary);
  const own = summary?.arms?.[arm]?.label;
  return (typeof own === "string" && own.trim()) || ARM_LABELS[arm] || arm;
}

export function armShortLabel(arm: string, summary?: ComparisonSummary | null): string {
  return ARM_SHORT[arm] || armLabel(arm, summary);
}

/** "Luccy, recorded 28 Sep": the recorded run's column, dated when the run says when. */
export function recordedLabel(summary?: ComparisonSummary | null): string {
  const day = fmtDay(summary?.luccy_recorded?.ts);
  return day ? `${ARM_LABELS[RECORDED]} ${day}` : ARM_LABELS[RECORDED];
}

/** The recorded run's figures on this run's questions, shaped like an arm's (older runs: null). */
export function recordedArm(summary?: ComparisonSummary | null): ComparisonArmSummary | null {
  const own = summary?.luccy_recorded?.on_this_run;
  return own && typeof own === "object" ? own : null;
}

/** The table columns: the arms, then Luccy's recorded run when the run carries it. */
export function withRecorded(arms: string[], summary?: ComparisonSummary | null): string[] {
  return recordedArm(summary) ? [...arms, RECORDED] : arms;
}

/** Every arm the run has (summary or rows), in the page's order. */
export function orderArms(summary?: ComparisonSummary | null, rows?: ComparisonRow[] | null): string[] {
  const seen: string[] = [];
  const add = (arm: string) => { if (!seen.includes(arm)) seen.push(arm); };
  Object.keys(summary?.arms ?? {}).forEach(add);
  for (const row of rows ?? []) Object.keys(row.arms ?? {}).forEach(add);
  const known = ARM_ORDER.filter((arm) => seen.includes(arm));
  return [...known, ...seen.filter((arm) => !(ARM_ORDER as readonly string[]).includes(arm))];
}

// ---- verdicts -----------------------------------------------------------------

export type VerdictTone = "same" | "better" | "worse" | "inconclusive" | "unknown";

const OUTCOMES: readonly VerdictTone[] = ["same", "better", "worse", "inconclusive"];

/** Whether `arm` did the same as, better or worse than `vs`, as the runner
 *  decided it (`outcome`); the page never re-decides. Only runs saved before
 *  the runner named an outcome fall back to its rule of the time: inside the
 *  noise floor is "same" whatever the sign of the difference. */
export function verdictTone(verdict?: Pick<ComparisonVerdict, "outcome" | "delta" | "threshold" | "within_noise"> | null): VerdictTone {
  if (!verdict) return "unknown";
  if (verdict.outcome !== undefined && verdict.outcome !== null) {
    const outcome = String(verdict.outcome).trim().toLowerCase() as VerdictTone;
    return OUTCOMES.includes(outcome) ? outcome : "unknown";
  }
  const { delta, threshold, within_noise: withinNoise } = verdict;
  if (withinNoise === true) return "same";
  if (withinNoise !== false && !(isNum(delta) && isNum(threshold))) return "unknown";
  if (!isNum(delta)) return "unknown";
  if (withinNoise !== false && Math.abs(delta) <= (threshold as number)) return "same";
  return delta > 0 ? "better" : delta < 0 ? "worse" : "same";
}

export interface ToneStyle { badge: string; card: string; text: string }

export const TONE_STYLES: Record<VerdictTone, ToneStyle> = {
  same: { badge: "Same", card: "border-ok/40 bg-ok/10", text: "text-ok" },
  better: { badge: "Better", card: "border-ok/40 bg-ok/10", text: "text-ok" },
  worse: { badge: "Worse", card: "border-err/40 bg-err/10", text: "text-err" },
  inconclusive: { badge: "Inconclusive", card: "border-run/40 bg-run/10", text: "text-run" },
  unknown: { badge: "Not enough data", card: "border-line bg-panel", text: "text-muted-foreground" },
};

/** The runner's sentence, or one built from the numbers when it has none. */
export function verdictText(verdict: ComparisonVerdict | null | undefined, summary?: ComparisonSummary | null): string {
  const own = verdict?.text;
  if (typeof own === "string" && own.trim()) return own.trim();
  if (!verdict) return "Not enough answers to compare yet.";
  const other = armLabel(verdict.vs, summary);
  const tone = verdictTone(verdict);
  if (tone === "same") return `Same as ${other} (within its own run-to-run variation)`;
  if (tone === "better") return `Better than ${other} by ${fmtQuestions(verdict.delta)}`;
  if (tone === "worse") return `Worse than ${other} by ${fmtQuestions(verdict.delta)}`;
  if (tone === "inconclusive") return `Not enough evidence to tell it apart from ${other}`;
  return "Not enough answers to compare yet.";
}

export function findVerdict(summary: ComparisonSummary | null | undefined, arm: string, vs: string): ComparisonVerdict | null {
  return summary?.verdicts?.find((v) => v.arm === arm && v.vs === vs) ?? null;
}

/** Arm names as a sentence says them. */
const ARM_SPOKEN: Record<string, string> = {
  A: "Luccy",
  A2: "Luccy's second run",
  C: "DataGov chat",
};

function spokenArm(arm: string, summary?: ComparisonSummary | null): string {
  return ARM_SPOKEN[arm] || armLabel(arm, summary);
}

/** "DataGov chat disagreed with Luccy on 12% of questions; Luccy disagreed with itself on 8%."
 *  Disagreeing = one of the two right, the other not; each side says
 *  "not measured" when the run has no number for it. */
export function disagreementSentence(verdict: ComparisonVerdict | null | undefined, summary?: ComparisonSummary | null): string {
  const who = spokenArm(verdict?.arm || "C", summary), other = spokenArm(verdict?.vs || "A", summary);
  const luccy = spokenArm("A", summary);
  const own = isNum(verdict?.disagree_rate)
    ? `${who} disagreed with ${other} on ${fmtPct(verdict.disagree_rate)} of questions`
    : `How often ${who} disagreed with ${other} was not measured`;
  const self = isNum(verdict?.luccy_disagree_rate)
    ? `${luccy} disagreed with itself on ${fmtPct(verdict.luccy_disagree_rate)}`
    : `how often ${luccy} disagrees with itself was not measured`;
  return `${own}; ${self}.`;
}

/** The 95% confidence interval of the difference in right answers, in
 *  percentage points: "95% CI −12 to +4 points"; "" without one. */
export function ciText(verdict?: Pick<ComparisonVerdict, "ci_low" | "ci_high"> | null): string {
  if (!isNum(verdict?.ci_low) || !isNum(verdict?.ci_high)) return "";
  return `95% CI ${fmtPoints(verdict.ci_low)} to ${fmtPoints(verdict.ci_high)} points`;
}

/** The runner found the arm disagreeing with luccy more than luccy does with itself. */
export function disagreesTooMuch(verdict?: Pick<ComparisonVerdict, "disagrees_more_than_luccy_itself"> | null): boolean {
  return verdict?.disagrees_more_than_luccy_itself === true;
}

export const NOISE_NOT_MEASURED = "Luccy's run-to-run variation was not measured in this run.";

/** "Luccy answered 9 questions (8%) differently when asked twice; differences up to ±6 questions are normal."
 *  Both counts are questions (noise_floor.flips and noise_floor.threshold);
 *  no noise floor means luccy was not run twice. */
export function noiseSentence(summary?: ComparisonSummary | null): string {
  const noise = summary?.noise_floor;
  if (!noise) return NOISE_NOT_MEASURED;
  const flips = isNum(noise.flips) ? fmtQuestions(noise.flips) : `${DASH} questions`;
  const share = isNum(noise.disagree_rate) ? ` (${fmtPct(noise.disagree_rate)})` : "";
  const threshold = isNum(noise.threshold) ? fmtQuestions(noise.threshold) : `${DASH} questions`;
  return `Luccy answered ${flips}${share} differently when asked twice; differences up to ±${threshold} are normal.`;
}

/** Whether a pair's difference could be chance, from McNemar's p; "" without one. */
export function chanceText(p: unknown): string {
  if (!isNum(p) || p < 0) return "";
  const shown = p < 0.001 ? "p < 0.001" : `p = ${p < 0.01 ? p.toFixed(3) : p.toFixed(2)}`;
  return p < 0.05 ? `The difference is unlikely to be chance (${shown}).` : `The difference could be chance (${shown}).`;
}

/** "multi_hop" → "Multi hop". */
export function kindLabel(kind: unknown): string {
  if (typeof kind !== "string" || !kind.trim()) return DASH;
  const words = kind.trim().replace(/[_-]+/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** One per-kind cell: "7 of 10" and "70%". */
export function kindCell(cell?: { n?: number | null; pass?: number | null } | null): { main: string; pct: string } {
  const n = cell?.n, pass = cell?.pass;
  return { main: fmtOf(pass, n), pct: isNum(pass) && isNum(n) && n > 0 ? fmtPct(pass / n) : DASH };
}

/** A run setting as text: primitives as they are, lists joined, anything else as JSON. */
export function paramText(value: unknown): string {
  if (value === null || value === undefined || value === "") return DASH;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && value.every((v) => ["string", "number", "boolean"].includes(typeof v))) return value.join(", ") || DASH;
  try {
    const text = JSON.stringify(value);
    return text.length > 200 ? `${text.slice(0, 199)}…` : text;
  } catch {
    return DASH;
  }
}

// ---- per-question answers -------------------------------------------------------

export interface AnswerStyle { label: string; className: string }

/** How one arm's answer to one question is shown. */
export function answerStyle(verdict: unknown): AnswerStyle {
  switch (verdict) {
    case "pass": return { label: "Right", className: "bg-ok/15 text-ok" };
    case "fail": return { label: "Wrong", className: "bg-err/15 text-err" };
    case "abstain": return { label: "Didn't know", className: "bg-run/15 text-run" };
    case "error": return { label: "Error", className: "bg-warn/15 text-warn" };
    default: return { label: DASH, className: "bg-foreground/[0.06] text-muted-foreground" };
  }
}

/** One arm's verdict on a question; Luccy's recorded run's is `row.recorded`.
 *  undefined when that arm (or the recorded run) has no verdict for it. */
export function rowVerdict(row: ComparisonRow, arm: string): unknown {
  if (arm === RECORDED) return row.recorded === "pass" || row.recorded === "fail" ? row.recorded : undefined;
  const leg = row.arms?.[arm];
  return leg && typeof leg === "object" ? leg.verdict ?? null : undefined;
}

/** Both arms were asked the question and exactly one of them got it right:
 *  pass against not-pass, as the agreement table counts it ("Wrong" against
 *  "Didn't know" is not a difference; an error counts as not right). */
export function differs(row: ComparisonRow, a: string, b: string): boolean {
  const left = rowVerdict(row, a), right = rowVerdict(row, b);
  if (left === undefined || right === undefined) return false;
  return (left === "pass") !== (right === "pass");
}

export type QuestionFilter = "all" | "c-vs-a" | "a-vs-recorded";

export const QUESTION_FILTERS: { id: QuestionFilter; label: string; arms: [string, string] | null }[] = [
  { id: "all", label: "All questions", arms: null },
  { id: "c-vs-a", label: "DataGov chat differs from Luccy", arms: ["C", "A"] },
  { id: "a-vs-recorded", label: "Luccy differs from its recorded run", arms: ["A", RECORDED] },
];

/** The arms the questions can be filtered by: the run's, plus the recorded run when a question has its verdict. */
export function filterArms(arms: string[], rows: ComparisonRow[]): string[] {
  return rows.some((row) => rowVerdict(row, RECORDED) !== undefined) ? [...arms, RECORDED] : arms;
}

/** The expected documents a question needs that DataGov does not hold. */
export function rowDocsMissing(row: ComparisonRow): string[] {
  return Array.isArray(row.expected_docs_missing) ? row.expected_docs_missing.filter((d): d is string => typeof d === "string" && d.length > 0) : [];
}

export function filterRows(rows: ComparisonRow[], filter: QuestionFilter): ComparisonRow[] {
  const arms = QUESTION_FILTERS.find((f) => f.id === filter)?.arms;
  return arms ? rows.filter((row) => differs(row, arms[0], arms[1])) : rows;
}

// ---- the golden set and Luccy's recorded run --------------------------------------

export interface GoldenSetLine { same: boolean; text: string }

/** "121 questions · the same set Luccy's recorded score used (digest 2670070ea44a)",
 *  or a warning when it is not that set. null for runs older than the check. */
export function goldenSetLine(summary?: ComparisonSummary | null): GoldenSetLine | null {
  const gold = summary?.gold;
  if (!gold || typeof gold !== "object") return null;
  const questions = isNum(gold.rows) ? plural(gold.rows, "question") : `${DASH} questions`;
  const digest = typeof gold.digest === "string" && gold.digest ? gold.digest : DASH;
  if (gold.same_as_recorded === true) {
    return { same: true, text: `${questions} · the same set Luccy's recorded score used (digest ${digest})` };
  }
  const recorded = typeof gold.baseline_digest === "string" && gold.baseline_digest ? gold.baseline_digest : DASH;
  const changed = Array.isArray(gold.changed_ids) ? gold.changed_ids.length : 0;
  return {
    same: false,
    text: `${questions} · not the set Luccy's recorded score used (digest ${digest}, recorded ${recorded})${changed ? `; ${plural(changed, "question")} changed since` : ""}`,
  };
}

/** Why a run is not the full golden set, or null: a smoke run that asked part
 *  of it, or a run that stopped before the end (a run still going is not). */
export function partialRunText(run?: Pick<ComparisonRunInfo, "summary" | "status" | "stale"> | null): string | null {
  const summary = run?.summary;
  if (!summary || summary.complete !== false) return null;
  const rows = isNum(summary.gold?.rows) ? summary.gold.rows : null;
  const selected = isNum(summary.gold?.selected) ? summary.gold.selected : null;
  const of = rows !== null ? `of the ${plural(rows, "golden question")}` : "of the golden questions";
  const after = " Its numbers can't be set against Luccy's recorded score on the full set.";
  if (rows !== null && selected !== null && selected < rows) return `This run asked ${fmtCount(selected)} ${of} (a smoke run).${after}`;
  if (isRunActive(run)) return null;
  return `This run stopped after ${fmtCount(summary.questions)} ${of}.${after}`;
}

/** "Luccy's recorded run (28 Sep 2026): 74 of 121 right (61.2%), facts covered 70.6%, judged by gemini-3.5-flash-lite." */
export function recordedSentence(summary?: ComparisonSummary | null): string | null {
  const rec = summary?.luccy_recorded;
  if (!rec || typeof rec !== "object") return null;
  const day = fmtDay(rec.ts, true);
  const judge = typeof rec.judge_model === "string" && rec.judge_model ? `, judged by ${rec.judge_model}` : "";
  return `Luccy's recorded run${day ? ` (${day})` : ""}: ${fmtOf(rec.n_pass, rec.n)} right (${fmtPct(rec.pass_rate)}), facts covered ${fmtPct(rec.fact_ratio)}${judge}.`;
}

/** "Luccy's recorded score (28 Sep) on the same questions: 74 of 121 right (61.2%)."; "" without one. */
export function recordedScoreSentence(summary?: ComparisonSummary | null): string {
  const own = recordedArm(summary);
  if (!own || !isNum(own.n) || own.n <= 0) return "";
  const day = fmtDay(summary?.luccy_recorded?.ts);
  return `Luccy's recorded score${day ? ` (${day})` : ""} on the same questions: ${fmtOf(own.pass, own.n)} right (${fmtPct(own.pass_rate)}).`;
}

/** Luccy's own measurement of its run-to-run noise on the golden set (its eval/gold/README.md). */
export const RECORDED_NOISE = "Two identical Luccy runs flip 22–25 of these questions between right and wrong, so a gap under about 7 right answers in a single run is noise.";

/** For a pair against the recorded run: how many questions flipped, against Luccy's own noise; "" otherwise. */
export function flipsText(pair?: Pick<ComparisonAgreement, "b" | "only_a" | "only_b"> | null): string {
  if (pair?.b !== RECORDED || !isNum(pair.only_a) || !isNum(pair.only_b)) return "";
  return `${plural(pair.only_a + pair.only_b, "question")} flipped against the recorded run; identical Luccy runs flip 22–25.`;
}

export interface DriftInfo { same: boolean; lines: string[]; documents: string }

/** Luccy's configuration against its recorded run, in plain words; null for older runs.
 *  The documents always differ: that line says so and is never counted as drift. */
export function configDrift(summary?: ComparisonSummary | null): DriftInfo | null {
  const drift = summary?.config_drift;
  if (!drift || typeof drift !== "object") return null;
  const lines = (Array.isArray(drift.differences) ? drift.differences : []).flatMap((d) => {
    if (!d || typeof d !== "object") return [];
    if (typeof d.text === "string" && d.text.trim()) return [d.text.trim()];
    return typeof d.field === "string" && d.field ? [`${d.field} changed since the recorded run.`] : [];
  });
  const count = drift.documents?.current?.documents;
  const documents = `Documents: Luccy's recorded run searched its own index; this run searches DataGov's copy${isNum(count) ? ` (${plural(count, "document")})` : ""}. This difference is expected: it is what the comparison measures.`;
  return { same: lines.length === 0, lines, documents };
}

export interface DocsMissing { id: string; question: string; missing: string[] }

/** The questions whose expected documents DataGov does not hold; null for older runs. */
export function docsMissing(value: unknown): DocsMissing[] | null {
  if (!Array.isArray(value)) return null;
  return value.flatMap((entry): DocsMissing[] => {
    const record = entry && typeof entry === "object" ? (entry as Record<string, unknown>) : null;
    if (!record || typeof record.id !== "string" || !record.id) return [];
    const missing = Array.isArray(record.missing) ? record.missing.filter((d): d is string => typeof d === "string" && d.length > 0) : [];
    return [{ id: record.id, question: typeof record.question === "string" ? record.question.trim() : "", missing }];
  });
}

// ---- left out of the scores (older runs) -----------------------------------------

export interface ExcludedQuestion { id: string; question: string }

/** An older run's excluded-questions list as id + question text (runs now
 *  leave out nothing but service failures). Entries are `{id, question}`;
 *  even older runs stored bare ids, whose text then comes from the run's rows
 *  when it is there. null when the run has no such list. */
export function excludedQuestions(value: unknown, rows?: ComparisonRow[] | null): ExcludedQuestion[] | null {
  if (!Array.isArray(value)) return null;
  const out: ExcludedQuestion[] = [];
  for (const entry of value) {
    const record = entry && typeof entry === "object" ? (entry as Record<string, unknown>) : null;
    const id = typeof entry === "string" ? entry : typeof record?.id === "string" ? record.id : "";
    if (!id) continue;
    const own = typeof record?.question === "string" ? record.question.trim() : "";
    const fromRows = rows?.find((row) => row.id === id)?.question;
    out.push({ id, question: own || (typeof fromRows === "string" ? fromRows.trim() : "") });
  }
  return out;
}

export interface InfraError { id: string; arm: string; message: string }

/** Answers that failed for infrastructure reasons (a timeout, a refused
 *  connection): which question, which arm, what happened. */
export function infraErrors(value: unknown): InfraError[] | null {
  if (!Array.isArray(value)) return null;
  return value.flatMap((entry): InfraError[] => {
    const record = entry && typeof entry === "object" ? (entry as Record<string, unknown>) : null;
    if (!record || typeof record.id !== "string" || !record.id) return [];
    return [{
      id: record.id,
      arm: typeof record.arm === "string" ? record.arm : "",
      message: typeof record.message === "string" && record.message.trim() ? record.message.trim() : DASH,
    }];
  });
}

// ---- runs ---------------------------------------------------------------------

const RUN_STATUS: Record<string, string> = {
  queued: "Waiting to start", pending: "Waiting to start", running: "Running",
  done: "Finished", finished: "Finished", completed: "Finished", complete: "Finished", success: "Finished",
  failed: "Failed", error: "Failed", cancelled: "Stopped", canceled: "Stopped", stopped: "Stopped", aborted: "Stopped",
};

export function runStatusLabel(status: unknown): string {
  if (typeof status !== "string" || !status.trim()) return DASH;
  const key = status.trim().toLowerCase();
  return RUN_STATUS[key] || key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " ");
}

/** A run the backend flags `stale`: still "running", but nothing has changed
 *  for 30 minutes, so its runner has died. It is shown stopped, not polled. */
export function isRunStale(run?: Pick<ComparisonRunInfo, "stale"> | null): boolean {
  return run?.stale === true;
}

export const STALE_RUN_LABEL = "Stopped — no progress for 30+ minutes";

/** Whether the run can still change, so the page keeps asking for it. */
export function isRunActive(run?: Pick<ComparisonRunInfo, "status" | "stale"> | null): boolean {
  if (isRunStale(run)) return false;
  const status = String(run?.status ?? "").toLowerCase();
  return status === "running" || status === "queued" || status === "pending";
}

/** Badge colours for a run status. */
export function runStatusClass(status: unknown): string {
  const label = runStatusLabel(status);
  if (label === "Finished") return "bg-ok/15 text-ok";
  if (label === "Failed") return "bg-err/15 text-err";
  if (label === "Running" || label === "Waiting to start") return "bg-run/15 text-run";
  return "bg-foreground/[0.06] text-muted-foreground";
}

/** A run's state in plain words, a stale run included. */
export function runLabel(run?: Pick<ComparisonRunInfo, "status" | "stale"> | null): string {
  return isRunStale(run) ? STALE_RUN_LABEL : runStatusLabel(run?.status);
}

export function runClass(run?: Pick<ComparisonRunInfo, "status" | "stale"> | null): string {
  return isRunStale(run) ? "bg-warn/15 text-warn" : runStatusClass(run?.status);
}

/** A run made of fake answers and a stand-in judge (the runner's --dry-run). */
export function isDryRun(run?: Pick<ComparisonRunInfo, "params"> | null): boolean {
  return run?.params?.dry_run === true;
}
