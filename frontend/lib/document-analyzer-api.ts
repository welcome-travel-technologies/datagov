/**
 * Client for the Document Analyzer: luccy's original answer engine behind
 * `/api/document-analyzer/`. Plain JSON calls go through `request`; the answer
 * stream uses fetch + a body reader, because EventSource can neither POST nor
 * send the CSRF header.
 */
import { ApiError, extractApiErrorMessage, request, unwrapResults } from "@/lib/api";
import { getCookie } from "@/lib/utils";

const BASE = "/api/document-analyzer";

export const ANALYZER_SOURCES = ["confluence", "jira", "drive", "github"] as const;
export type AnalyzerSource = (typeof ANALYZER_SOURCES)[number];
export const SOURCE_LABELS: Record<AnalyzerSource, string> = {
  confluence: "Confluence", jira: "Jira", drive: "Drive", github: "GitHub",
};
export type AnalyzerStyle = "" | "plain";

/** What the page shows while an answer runs. Luccy's own STAGE_LABELS
 *  (serving/api/page_routes.py), so the wording matches the standalone app. */
export const STAGE_LABELS: Record<string, string> = {
  starting: "Starting…",
  routing: "Reading the question…",
  decompose: "Planning…",
  retrieve: "Searching the corpus…",
  retrieve_warehouse: "Searching the corpus and the warehouse…",
  warehouse: "Querying the warehouse…",
  deep: "Reading the documents more closely…",
  synthesize: "Writing the answer…",
  arith: "Double-checking the numbers…",
  verifying: "Double-checking the numbers…",
};

export interface AnalyzerStatus {
  enabled: boolean;
  configured: boolean;
  /** This user may use the page. */
  available: boolean;
  /** A quick health probe of the engine; null when it is not configured. */
  healthy: boolean | null;
  /** Plain-words explanation when not available ("" otherwise). */
  reason: string;
  sources: string[];
  tool_available: boolean;
}

export interface AnalyzerCitation { id: string; title: string; url: string | null }

/** The `answer` / `done` event payload (luccy's answer_payload). */
export interface AnalyzerAnswer {
  log: string | null;
  answer: string;
  /** The same rendered by luccy; only its old-documents warning is read. */
  answer_html?: string;
  footer: string;
  citations: AnalyzerCitation[];
  abstained: boolean;
  gaps: string[];
  early?: boolean;
  cut?: boolean;
}

export interface AnalyzerDocument {
  id: string;
  title: string;
  source: string;
  modified: string;
  url: string | null;
  path: string[];
  body_html: string;
  passages: { heading: string; text: string }[];
}

export interface HistoryTurn { who: "you" | "luccy"; text: string }

export interface AskBody {
  question: string;
  conversation?: string;
  sources?: AnalyzerSource[];
  style?: AnalyzerStyle;
  history?: HistoryTurn[];
}

export type FeedbackVerdict = "right" | "wrong" | "cleared";

/** Luccy's answer reference (`log`, "ref" in the footer): 8 lowercase hex. */
export const ANSWER_ID_RE = /^[0-9a-f]{8}$/;
// The backend's rule (luccy's own): a leading letter or digit, never "..".
const DOCUMENT_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,299}$/;
const JOB_RE = /^[A-Za-z0-9_-]{1,64}$/;

/** Whether a citation id can be opened in the document panel. Ids the
 *  backend refuses (a URL would treat dots as path segments) stay labels. */
export function isOpenableDocumentId(id: string): boolean {
  return DOCUMENT_ID_RE.test(id) && !id.includes("..") && !isWarehouse(id);
}

/** Warehouse sources (`wh:`) are live data, not a page: a plain label. */
export function isWarehouse(id: string): boolean {
  return id.startsWith("wh:");
}

/** The system a cited page came from, by luccy's id/url rule (chat.html citeSource). */
export function citationSource(citation: Pick<AnalyzerCitation, "id" | "url">): AnalyzerSource | null {
  const id = citation.id, url = citation.url || "";
  if (id.startsWith("gd")) return "drive";
  if (id.startsWith("PBI-")) return null;
  if (url.includes("://github.com/")) return "github";
  if (url.includes("/wiki/") || /^\d+$/.test(id)) return "confluence";
  return "jira";
}

/** Link comparison as luccy does it: trimmed, no trailing slash, lower case. */
export function normalizeUrl(value: string | null | undefined): string {
  return String(value || "").trim().replace(/\/+$/, "").toLowerCase();
}

/** Answer markdown with images switched off, as luccy renders it (render.py
 *  disables them): a cited page could steer the model into an image whose URL
 *  carries the question, and the browser would fetch it unasked. Every `!`
 *  opening an image gets a backslash, unless an odd run of backslashes already
 *  escapes it, so `![alt](url)` shows as "!" and a link. Inside code the added
 *  backslash shows; images never do. */
export function withoutImages(markdown: string): string {
  return markdown.replace(/(\\*)!\[/g, (match, slashes: string) => (slashes.length % 2 ? match : `${slashes}\\![`));
}

// render.py appends this line to answer_html only (an older wording in saved answers).
const STALE_RE = /<p>⚠ (?:These documents are|This context index is) (\d{1,6}) days old \((?:copied|built) (\d{4}-\d{2}-\d{2})[^<]*<\/p>\s*$/;

/** Luccy's "documents are N days old" warning, rebuilt as plain text from the
 *  end of its rendered answer; "" when the documents are recent. */
export function staleWarning(answerHtml: unknown): string {
  const match = typeof answerHtml === "string" ? STALE_RE.exec(answerHtml) : null;
  return match ? `⚠ These documents are ${match[1]} days old (copied ${match[2]}) — recent changes may not be reflected.` : "";
}

// ---- comparison runs (org admins) --------------------------------------------
// Every field is optional on purpose: the page shows "—" for whatever a run
// has not produced (yet), and never trusts the JSON to be complete.

/** One arm's headline numbers. Rates are fractions (0.72 = 72%). */
export interface ComparisonArmSummary {
  label?: string;
  /** Questions this arm was asked. */
  n?: number | null;
  answered?: number | null;
  pass?: number | null;
  pass_rate?: number | null;
  expected_doc_hit_rate?: number | null;
  fact_recall?: number | null;
  correct_abstentions?: number | null;
  abstain_rows?: number | null;
  privacy_leaks?: number | null;
  errors?: number | null;
  p50_latency_s?: number | null;
  p90_latency_s?: number | null;
  est_cost_usd?: number | null;
  /** Questions this arm could not answer because a service failed (left out of its scores). */
  infra_errors?: number | null;
  /** How est_cost_usd was obtained: only "measured" is an actual bill. */
  cost_source?: CostSource | string | null;
}

export type CostSource = "measured" | "estimate" | "mixed";

/** Pass/fail agreement between arms `a` and `b` over the same questions. */
export interface ComparisonAgreement {
  a: string;
  b: string;
  both_pass?: number | null;
  only_a?: number | null;
  only_b?: number | null;
  both_fail?: number | null;
  mcnemar_p?: number | null;
}

export type VerdictOutcome = "same" | "better" | "worse" | "inconclusive";

/** `arm` against `vs`, as the runner decided it. `outcome` and `text` are the
 *  runner's; the page shows them and never re-decides. `delta` and `threshold`
 *  are whole question counts: delta = pass(arm) − pass(vs) (positive = arm
 *  better); threshold is the run-to-run variation of luccy itself, in
 *  questions. Every *_rate, ci_low/ci_high and margin is a fraction
 *  (0.05 = 5 percentage points). */
export interface ComparisonVerdict {
  arm: string;
  vs: string;
  outcome?: VerdictOutcome | string | null;
  /** Questions both arms answered. */
  n?: number | null;
  delta?: number | null;
  threshold?: number | null;
  /** pass_rate(arm) − pass_rate(vs), with its 95% confidence interval. */
  diff_rate?: number | null;
  ci_low?: number | null;
  ci_high?: number | null;
  margin?: number | null;
  /** Share of the questions where one arm was right and the other not. */
  disagree_rate?: number | null;
  /** The same for luccy against its own second run; null without A2. */
  luccy_disagree_rate?: number | null;
  disagrees_more_than_luccy_itself?: boolean | null;
  within_noise?: boolean | null;
  text?: string | null;
}

/** A question older runs left out of the scores. Even older ones stored the bare id. */
export interface ComparisonExcludedQuestion { id: string; question?: string | null }

/** One arm's answer to one question that failed for infrastructure reasons. */
export interface ComparisonInfraError { id: string; arm?: string | null; message?: string | null }

/** Which golden set the run asked: luccy's gold set (`rows` questions, luccy's own digest of
 *  it) against the one luccy's recorded score used (`baseline_digest`). `selected` questions
 *  were asked; `changed_ids` have no recorded verdict for them as they are now. */
export interface ComparisonGold {
  rows?: number | null;
  selected?: number | null;
  digest?: string | null;
  baseline_digest?: string | null;
  same_as_recorded?: boolean | null;
  changed_ids?: string[] | null;
}

/** Luccy's recorded run on the golden set (its quality-gate baseline), as a reference.
 *  `on_this_run`: its figures on the questions this run finished, in an arm's shape. */
export interface ComparisonRecordedRun {
  ts?: string | null;
  n?: number | null;
  n_pass?: number | null;
  pass_rate?: number | null;
  fact_ratio?: number | null;
  judge_model?: string | null;
  judge_votes?: number | null;
  gold_digest?: string | null;
  latency_p50_s?: number | null;
  latency_p90_s?: number | null;
  usd?: number | null;
  pass_by_id?: Record<string, boolean> | null;
  on_this_run?: ComparisonArmSummary | null;
}

/** One configuration difference between luccy now and its recorded run, in plain words. */
export interface ComparisonDriftItem { field?: string | null; recorded?: unknown; current?: unknown; text?: string | null }

/** Luccy's configuration against its recorded run. The documents always differ (DataGov's
 *  copy against luccy's own index): `documents` says so, and it is not drift. */
export interface ComparisonConfigDrift {
  same?: boolean | null;
  differences?: ComparisonDriftItem[] | null;
  documents?: { text?: string | null; recorded?: Record<string, unknown> | null; current?: Record<string, unknown> | null } | null;
}

/** A question whose expected documents DataGov does not hold (asked and scored anyway). */
export interface ComparisonDocsMissing { id: string; question?: string | null; missing?: string[] | null }

export interface ComparisonSummary {
  questions?: number | null;
  arms?: Record<string, ComparisonArmSummary>;
  by_kind?: Record<string, Record<string, { n?: number | null; pass?: number | null }>>;
  agreement?: ComparisonAgreement[];
  /** How many questions luccy answered differently when asked twice (A vs A2),
   *  and the difference, in questions, that is therefore normal. null when
   *  luccy was not run twice. */
  noise_floor?: { flips?: number | null; threshold?: number | null; disagree_rate?: number | null } | null;
  verdicts?: ComparisonVerdict[];
  /** Only infrastructure failures are left out; `warehouse` and `not_in_corpus` are older runs'. */
  excluded?: {
    warehouse?: (ComparisonExcludedQuestion | string)[];
    not_in_corpus?: (ComparisonExcludedQuestion | string)[];
    infra_error?: ComparisonInfraError[];
  } | null;
  spent_usd?: number | null;
  /** Every golden question was asked and finished (false for a smoke run or a run stopped early). */
  complete?: boolean | null;
  gold?: ComparisonGold | null;
  luccy_recorded?: ComparisonRecordedRun | null;
  config_drift?: ComparisonConfigDrift | null;
  expected_docs_missing?: ComparisonDocsMissing[] | null;
}

export type ArmVerdict = "pass" | "fail" | "abstain" | "error";

export interface ComparisonRowArm {
  verdict?: ArmVerdict | string | null;
  /** The answer failed for infrastructure reasons (left out of the paired statistics). */
  infra_error?: boolean | null;
  cited?: string[] | null;
  answer?: string | null;
  latency_s?: number | null;
  fact_ratio?: number | null;
}

export interface ComparisonRow {
  id: string;
  kind?: string | null;
  question?: string | null;
  /** The order the arms were asked this question in. */
  order?: string[] | null;
  arms?: Record<string, ComparisonRowArm>;
  /** Expected documents DataGov does not hold (the question is scored anyway). */
  expected_docs_missing?: string[] | null;
  /** Luccy's recorded verdict on this question; null when it has none for it as it is now. */
  recorded?: "pass" | "fail" | null;
}

export interface ComparisonRunInfo {
  run_id: string;
  status: string;
  /** Still "running" but no update for 30 minutes: the runner has died. */
  stale?: boolean | null;
  /** `dry_run: true` marks a run made of fake answers. */
  params?: Record<string, unknown> | null;
  summary?: ComparisonSummary | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ComparisonRun extends ComparisonRunInfo {
  rows?: ComparisonRow[] | null;
}

const RUN_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

/** A run id that can go into the URL path as one segment. */
export function isValidRunId(id: string): boolean {
  return RUN_ID_RE.test(id) && !id.includes("..");
}

/** 403 or 404 from the comparison endpoints: not an org admin, or switched
 *  off. The page then hides the comparison view instead of showing an error. */
export function isComparisonHidden(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.status === 404);
}

// ---- web chat answers ---------------------------------------------------------

/** What DataGov's chat API says about a stored message: `verbatim` is true when
 *  the message is the Document Analyzer's own answer, stored as luccy wrote it
 *  and closed by the line "Answered by the Document Analyzer · ref <log>" (or
 *  "· ref -"). null or absent for every other message. */
export interface ChatAnalyzerMeta {
  verbatim?: boolean | null;
  ref?: string | null;
}

/** Whether a stored chat message is a verbatim Document Analyzer answer. Only
 *  the API's flag decides; what the text says never does. */
export function isVerbatimAnalyzerAnswer(meta: ChatAnalyzerMeta | null | undefined): boolean {
  return meta?.verbatim === true;
}

/** Splits a verbatim analyzer answer into its markdown and its closing line,
 *  which the backend always writes last. Call it only for verbatim messages. */
export function splitClosingLine(content: string): { body: string; footnote: string } {
  const text = String(content ?? "").replace(/\s+$/, "");
  const cut = Math.max(text.lastIndexOf("\n"), text.lastIndexOf("\r"));
  return { body: text.slice(0, cut + 1).replace(/\s+$/, ""), footnote: text.slice(cut + 1).trim() };
}

export const analyzerApi = {
  status: () => request<AnalyzerStatus>("/document-analyzer/status/"),
  /** Comparison runs, newest first (org admins; 403/404 otherwise). */
  comparisons: async () => unwrapResults(await request<ComparisonRunInfo[] | { results: ComparisonRunInfo[] }>("/document-analyzer/comparisons/")),
  comparison: (runId: string) => {
    if (!isValidRunId(runId)) return Promise.reject(new ApiError(404, "Unknown comparison run."));
    return request<ComparisonRun>(`/document-analyzer/comparisons/${encodeURIComponent(runId)}/`);
  },
  document: (id: string, answerId?: string | null) => {
    if (!isOpenableDocumentId(id)) return Promise.reject(new ApiError(404, "Unknown document."));
    const answer = answerId && ANSWER_ID_RE.test(answerId) ? `?answer=${answerId}` : "";
    return request<AnalyzerDocument>(`/document-analyzer/documents/${encodeURIComponent(id)}/${answer}`);
  },
  feedback: (answerId: string, verdict: FeedbackVerdict, note = "") =>
    request<{ ok: boolean }>("/document-analyzer/feedback/", {
      method: "POST",
      body: JSON.stringify({ answer_id: answerId, verdict, note: verdict === "cleared" ? "" : note.slice(0, 500) }),
    }),
};

// ---- conversation helpers ---------------------------------------------------

export const HISTORY_MAX_TURNS = 50;
export const LUCCY_TURN_CAP = 4000;
const HISTORY_TEXT_MAX = 16000;
const HISTORY_TOTAL_MAX = 64000;

/** Earlier turns condensed as luccy does (jobs.condense_local): empty turns
 *  dropped, the last luccy turn kept up to 4000 characters, earlier luccy
 *  turns cut to their first line, the last 50 turns. Then kept inside the
 *  API's limits (16,000 characters a turn, 64,000 in all), oldest first out. */
export function condenseHistory(turns: HistoryTurn[]): HistoryTurn[] {
  const raw = turns
    .map((t): HistoryTurn => ({ who: t.who === "luccy" ? "luccy" : "you", text: String(t.text || "").trim() }))
    .filter((t) => t.text);
  const lastLuccy = raw.map((t) => t.who).lastIndexOf("luccy");
  const out = raw
    .map((t, i): HistoryTurn => t.who !== "luccy" ? t
      : { who: "luccy", text: i === lastLuccy ? t.text.slice(0, LUCCY_TURN_CAP) : t.text.split("\n", 1)[0].trim() })
    .map((t) => ({ ...t, text: t.text.slice(0, HISTORY_TEXT_MAX) }))
    .slice(-HISTORY_MAX_TURNS);
  let total = out.reduce((n, t) => n + t.text.length, 0);
  while (out.length && total > HISTORY_TOTAL_MAX) total -= out.shift()!.text.length;
  return out;
}

/** Client conversation ids starting with "dg" (any case) are DataGov's own:
 *  the backend refuses them with 400. */
export function isReservedConversationId(id: string): boolean {
  return id.slice(0, 2).toLowerCase() === "dg";
}

/** A conversation id luccy accepts: "p" and 23 random [a-z0-9] (24 in all),
 *  so it never starts with DataGov's reserved "dg". */
export function newConversationId(): string {
  const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
  const bytes = new Uint8Array(23);
  try {
    crypto.getRandomValues(bytes);
  } catch {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  return "p" + Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

// ---- server-sent events -----------------------------------------------------

export interface SseMessage {
  event: string;
  data: string;
  /** The last event id seen, carried over from earlier events as the spec says. */
  id: string;
}

/** Incremental `text/event-stream` parser following the WHATWG rules: lines end
 *  in CRLF, LF or CR and may be split anywhere across chunks (a CRLF too);
 *  `data:` lines join with "\n"; `:` lines are comments (luccy's `: ping`);
 *  one space after the colon is dropped; a block without data is not an event;
 *  an event cut off by the end of the stream is never dispatched. */
export class SseParser {
  private buffer = "";
  private started = false;
  private skipLf = false;
  private event = "";
  private data = "";
  private idBuffer = "";
  /** Id of the last dispatched event: what a re-attach sends as Last-Event-ID. */
  lastEventId = "";

  constructor(
    private readonly onMessage: (message: SseMessage) => void,
    private readonly onComment?: (text: string) => void,
  ) {}

  push(chunk: string): void {
    let text = chunk;
    if (!text) return;
    if (!this.started) {
      this.started = true;
      if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    }
    if (this.skipLf) {
      this.skipLf = false;
      if (text[0] === "\n") text = text.slice(1);
    }
    this.buffer += text;
    const ends = /\r\n|\r|\n/g;
    let start = 0;
    for (let match = ends.exec(this.buffer); match; match = ends.exec(this.buffer)) {
      // A CR closing the buffer may be the first half of a CRLF split across chunks.
      if (match[0] === "\r" && match.index === this.buffer.length - 1) this.skipLf = true;
      const line = this.buffer.slice(start, match.index);
      start = match.index + match[0].length;
      this.line(line);
    }
    this.buffer = this.buffer.slice(start);
  }

  /** The stream ended: whatever is pending is dropped. */
  end(): void {
    this.buffer = "";
    this.event = "";
    this.data = "";
    this.idBuffer = this.lastEventId;
    this.skipLf = false;
  }

  private line(line: string) {
    if (line === "") return this.dispatch();
    if (line[0] === ":") {
      this.onComment?.(line.slice(1).replace(/^ /, ""));
      return;
    }
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value[0] === " ") value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data += value + "\n";
    else if (field === "id") { if (!value.includes("\0")) this.idBuffer = value; }
    // `retry` and unknown fields are ignored.
  }

  private dispatch() {
    this.lastEventId = this.idBuffer;
    const event = this.event || "message", data = this.data;
    this.event = "";
    this.data = "";
    if (!data) return;
    this.onMessage({ event, data: data.slice(0, -1), id: this.lastEventId });
  }
}

/** Reads a stream body through `SseParser`. `onMessage` returns true to stop
 *  (the answer ended); the result says whether that happened before the end of
 *  the stream. A broken connection rejects. */
export async function readEventStream(
  body: ReadableStream<Uint8Array>,
  onMessage: (message: SseMessage) => boolean | void,
  onComment?: (text: string) => void,
): Promise<boolean> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let stopped = false;
  const parser = new SseParser((message) => {
    if (!stopped && onMessage(message) === true) stopped = true;
  }, onComment);
  try {
    while (!stopped) {
      const { value, done } = await reader.read();
      if (done) {
        parser.push(decoder.decode());
        parser.end();
        break;
      }
      parser.push(decoder.decode(value, { stream: true }));
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
  return stopped;
}

/** The connection dropped and re-attaching did not bring the answer back. */
export class StreamDroppedError extends Error {
  constructor() {
    super("The connection dropped before the answer finished.");
    this.name = "StreamDroppedError";
  }
}

export function isAbortError(error: unknown): boolean {
  return (error as { name?: string } | null)?.name === "AbortError";
}

async function openStream(path: string, init: RequestInit): Promise<ReadableStream<Uint8Array>> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "include",
    cache: "no-store",
    headers: { Accept: "text/event-stream", ...((init.headers as Record<string, string>) ?? {}) },
  });
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}) as Record<string, unknown>);
    const status = res.ok ? 502 : res.status;
    throw new ApiError(status, extractApiErrorMessage(body, res.statusText || `Request failed (${status})`), body);
  }
  return res.body;
}

/** POST ask/stream/: starts an answer. Refusals throw ApiError with the
 *  backend's `detail` (400, 429 spend cap, 503 busy, 502, 504, 403, 404). */
export function openAskStream(body: AskBody, signal?: AbortSignal) {
  const csrf = getCookie("csrftoken");
  return openStream("/ask/stream/", {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRFToken": csrf } : {}) },
    body: JSON.stringify(body),
  });
}

/** GET ask/stream/<job>/: re-attaches to a running answer after `lastEventId`. */
export function openJobStream(job: string, lastEventId?: string, signal?: AbortSignal) {
  if (!JOB_RE.test(job)) return Promise.reject(new ApiError(404, "Unknown job."));
  return openStream(`/ask/stream/${encodeURIComponent(job)}/`, {
    method: "GET",
    signal,
    headers: lastEventId ? { "Last-Event-ID": lastEventId } : {},
  });
}

const abortReason = (signal: AbortSignal): unknown => signal.reason ?? new DOMException("Aborted", "AbortError");

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(abortReason(signal)); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  });
}

export interface StreamPosition { job: string; lastEventId: string }

export interface FollowOptions {
  /** Start a new answer with this question… */
  ask?: AskBody;
  /** …or re-attach to one already running (a reload mid-answer). */
  resume?: StreamPosition;
  signal: AbortSignal;
  /** Every event with JSON data; pings and unreadable data are skipped. */
  onEvent: (name: string, data: Record<string, unknown>, position: StreamPosition) => void;
  retries?: number;
  retryDelayMs?: number;
}

/** Follows one answer to its `done` or `error` event. When the connection
 *  drops first it re-attaches with GET ask/stream/<job>/ and Last-Event-ID, up
 *  to `retries` times `retryDelayMs` apart (luccy's page does 1.5 s). Rejects
 *  with ApiError for a refusal (404 on re-attach = the job is gone),
 *  StreamDroppedError when re-attaching did not help, or the abort reason. */
export async function followAnswer({ ask, resume, signal, onEvent, retries = 3, retryDelayMs = 1500 }: FollowOptions): Promise<void> {
  const position: StreamPosition = { job: resume?.job ?? "", lastEventId: resume?.lastEventId ?? "" };
  let body: ReadableStream<Uint8Array> | null = ask ? await openAskStream(ask, signal) : null;
  const handle = (message: SseMessage) => {
    if (signal.aborted) return true;
    if (message.id) position.lastEventId = message.id;
    let data: unknown;
    try { data = JSON.parse(message.data); } catch { return false; }
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    const record = data as Record<string, unknown>;
    if (!position.job && typeof record.job === "string" && JOB_RE.test(record.job)) position.job = record.job;
    onEvent(message.event, record, { ...position });
    return message.event === "done" || message.event === "error";
  };
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (!body) {
      if (!position.job) break;
      if (attempt > 0) await wait(retryDelayMs, signal);
      try {
        body = await openJobStream(position.job, position.lastEventId, signal);
      } catch (error) {
        // An unknown job, or a refused user, will not get better by retrying.
        if (signal.aborted) throw abortReason(signal);
        if (error instanceof ApiError && error.status < 500) throw error;
        continue;
      }
    }
    const current = body;
    body = null;
    let ended = false;
    try {
      ended = await readEventStream(current, handle);
    } catch {
      // A dropped connection: re-attach on the next turn, unless the caller aborted.
    }
    if (signal.aborted) throw abortReason(signal);
    if (ended) return;
  }
  if (signal.aborted) throw abortReason(signal);
  throw new StreamDroppedError();
}
