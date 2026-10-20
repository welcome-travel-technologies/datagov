/**
 * The Document Analyzer conversation, kept per user in localStorage (luccy
 * keeps its chats in the browser too). Every storage access is guarded: the
 * page works without storage (private mode, blocked site data, quota).
 */
import {
  ANALYZER_SOURCES,
  ANSWER_ID_RE,
  condenseHistory,
  isReservedConversationId,
  newConversationId,
  type AnalyzerCitation,
  type AnalyzerSource,
  type AnalyzerStyle,
  type FeedbackVerdict,
  type HistoryTurn,
} from "@/lib/document-analyzer-api";

export interface TurnAnswer {
  text: string;
  footer: string;
  citations: AnalyzerCitation[];
  gaps: string[];
  /** Luccy's answer reference, used for feedback and the document panel. */
  log: string | null;
  /** The answer is shown while the number check still runs (before `done`). */
  checking: boolean;
  /** The stream hit luccy's time limit: the check goes on without us. */
  cut: boolean;
  /** Luccy's warning that the documents behind the answer are old. */
  stale?: string;
}

export interface Turn {
  id: string;
  question: string;
  /** Sources asked with, when not all of them. */
  sources?: AnalyzerSource[];
  style: AnalyzerStyle;
  started: number;
  pending: boolean;
  stage?: string;
  /** Luccy's job id and last event id, so a reload can re-attach. */
  job?: string;
  lastEventId?: string;
  answer?: TurnAnswer;
  error?: string;
  feedback?: { verdict: FeedbackVerdict; note: string };
}

export interface Thread {
  conversation: string;
  turns: Turn[];
  sources: AnalyzerSource[];
  style: AnalyzerStyle;
}

/** Turns kept in storage: luccy's history window. */
export const MAX_STORED_TURNS = 50;

export const threadKey = (userId: number | string) => `datagov:analyzer:${userId}`;

export function newThread(): Thread {
  return { conversation: newConversationId(), turns: [], sources: [...ANALYZER_SOURCES], style: "" };
}

const str = (value: unknown, max = 100_000) => (typeof value === "string" ? value.slice(0, max) : "");
const isSource = (value: unknown): value is AnalyzerSource => ANALYZER_SOURCES.includes(value as AnalyzerSource);

function sourcesOf(value: unknown): AnalyzerSource[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const list = ANALYZER_SOURCES.filter((s) => value.includes(s));
  return list.length ? list : undefined;
}

export function citationsOf(value: unknown): AnalyzerCitation[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((c) => {
    if (!c || typeof c !== "object") return [];
    const { id, title, url } = c as Record<string, unknown>;
    if (typeof id !== "string" || !id) return [];
    return [{ id, title: str(title, 500), url: typeof url === "string" && url ? url : null }];
  });
}

export function gapsOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((g): g is string => typeof g === "string" && !!g.trim()) : [];
}

export function answerIdOf(value: unknown): string | null {
  return typeof value === "string" && ANSWER_ID_RE.test(value) ? value : null;
}

function turnOf(value: unknown): Turn | null {
  if (!value || typeof value !== "object") return null;
  const t = value as Record<string, unknown>;
  const question = str(t.question, 10_000);
  if (!question || typeof t.id !== "string") return null;
  const a = t.answer as Record<string, unknown> | undefined;
  const f = t.feedback as Record<string, unknown> | undefined;
  return {
    id: t.id,
    question,
    sources: sourcesOf(t.sources),
    style: t.style === "plain" ? "plain" : "",
    started: typeof t.started === "number" ? t.started : 0,
    pending: t.pending === true,
    stage: str(t.stage, 64) || undefined,
    job: str(t.job, 64) || undefined,
    lastEventId: str(t.lastEventId, 64) || undefined,
    answer: a && typeof a === "object" ? {
      text: str(a.text), footer: str(a.footer, 1000), citations: citationsOf(a.citations), gaps: gapsOf(a.gaps),
      log: answerIdOf(a.log), checking: a.checking === true, cut: a.cut === true, stale: str(a.stale, 300) || undefined,
    } : undefined,
    error: str(t.error, 1000) || undefined,
    feedback: f && ["right", "wrong", "cleared"].includes(f.verdict as string)
      ? { verdict: f.verdict as FeedbackVerdict, note: str(f.note, 500) } : undefined,
  };
}

/** The saved thread, or a new one when there is none or it cannot be read. */
export function loadThread(userId: number | string): Thread {
  try {
    return parseThread(localStorage.getItem(threadKey(userId)));
  } catch {
    return newThread();
  }
}

/** A thread from its stored JSON (null or unreadable: a new thread). */
export function parseThread(raw: string | null | undefined): Thread {
  try {
    if (!raw) return newThread();
    const value = JSON.parse(raw) as Record<string, unknown>;
    // A saved id in DataGov's reserved "dg" namespace would be refused (400) on every question.
    const conversation = typeof value.conversation === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value.conversation)
      && !isReservedConversationId(value.conversation) ? value.conversation : "";
    if (!conversation) return newThread();
    const turns = (Array.isArray(value.turns) ? value.turns : []).map(turnOf).filter((t): t is Turn => !!t);
    const sources = Array.isArray(value.sources) ? value.sources.filter(isSource) : [];
    return {
      conversation,
      turns: turns.slice(-MAX_STORED_TURNS),
      sources: sources.length ? ANALYZER_SOURCES.filter((s) => sources.includes(s)) : [...ANALYZER_SOURCES],
      style: value.style === "plain" ? "plain" : "",
    };
  } catch {
    return newThread();
  }
}

export function saveThread(userId: number | string, thread: Thread) {
  try {
    localStorage.setItem(threadKey(userId), JSON.stringify({ ...thread, turns: thread.turns.slice(-MAX_STORED_TURNS) }));
  } catch {
    /* storage unavailable or full: the thread still works for this visit */
  }
}

/** Every analyzer thread in this browser, gone (for sign-out: the answers
 *  quote company documents). Other open tabs follow through `syncThread`. */
export function clearAnalyzerThreads() {
  try {
    const keys = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i))
      .filter((key): key is string => !!key?.startsWith(threadKey("")));
    keys.forEach((key) => localStorage.removeItem(key));
  } catch {
    /* storage unavailable: nothing was kept */
  }
}

/** Another tab saved the thread: this tab follows it, so neither overwrites
 *  the other's turns. The answer this tab is streaming (`running`) is kept
 *  while the other tab is in the same chat; a new chat there wins. */
export function syncThread(current: Thread, incoming: Thread, running: string | null): Thread {
  const mine = running ? current.turns.find((t) => t.id === running) : undefined;
  if (!mine || incoming.conversation !== current.conversation) return incoming;
  const turns = incoming.turns.filter((t) => t.id !== mine.id).concat(mine).sort((a, b) => a.started - b.started);
  return { ...incoming, turns: turns.slice(-MAX_STORED_TURNS) };
}

/** The history sent with a new question: every earlier answered turn. */
export function historyOf(turns: Turn[]): HistoryTurn[] {
  return condenseHistory(turns.flatMap((t): HistoryTurn[] => t.answer && !t.pending
    ? [{ who: "you", text: t.question }, { who: "luccy", text: t.answer.text }] : []));
}
