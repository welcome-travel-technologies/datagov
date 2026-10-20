"use client";

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, FileSearch, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ApiError, extractApiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  ANALYZER_SOURCES,
  followAnswer,
  SOURCE_LABELS,
  staleWarning,
  StreamDroppedError,
  type AnalyzerCitation,
  type AnalyzerSource,
  type AnalyzerStatus,
  type AskBody,
  type FeedbackVerdict,
  type StreamPosition,
} from "@/lib/document-analyzer-api";
import { AnswerView, SIGNED_OUT, StageLine, stageLabel } from "./answer-view";
import { DocumentPanel, type PanelTarget } from "./document-panel";
import {
  answerIdOf,
  citationsOf,
  gapsOf,
  historyOf,
  loadThread,
  MAX_STORED_TURNS,
  newThread,
  parseThread,
  saveThread,
  syncThread,
  threadKey,
  type Thread,
  type Turn,
} from "./thread";

// What a reader is told when an answer fails: plain words, never a status code.
const DISCONNECT = "Can't reach the Document Analyzer right now — check your internet connection.";
const DROPPED = "The connection dropped before the answer finished. Please ask the question again.";
const CANCELLED = "This answer was cancelled.";
const INTERRUPTED = "This answer was cut off. Please ask the question again.";
const ANSWER_FAILED = "The Document Analyzer had a problem answering. Please try again in a minute.";
const UNAVAILABLE = "The Document Analyzer isn't available for your account right now.";

const PLACEHOLDER = "Ask about company docs, metrics, decisions…  (Enter to send · Shift+Enter for a new line)";
const BUSY_PLACEHOLDER = "The Document Analyzer is answering this chat… start a new chat to ask something else";
const LAST_SOURCE_HINT = "At least one source stays on.";

/** Words for a failed stream. `attached`: the answer had started, so a 404 is
 *  a job luccy no longer has rather than a disabled feature. */
export function failureText(error: unknown, attached: boolean): string {
  if (error instanceof StreamDroppedError) return DROPPED;
  if (error instanceof ApiError) {
    if (error.status === 401) return SIGNED_OUT;
    // The backend's 404 ("Not found.") means switched off or no longer for this user.
    if (error.status === 404) return attached ? INTERRUPTED : UNAVAILABLE;
    return extractApiErrorMessage(error.body, "") || ANSWER_FAILED;
  }
  if (error instanceof TypeError) return DISCONNECT;
  return ANSWER_FAILED;
}

/** The turn after a failure: an answer already on screen stays, its number
 *  check marked unfinished; otherwise the failure text replaces the stage line. */
function failTurn(turn: Turn, message: string): Turn {
  if (turn.answer) {
    return { ...turn, pending: false, stage: undefined, answer: { ...turn.answer, checking: false, cut: turn.answer.cut || turn.answer.checking } };
  }
  return { ...turn, pending: false, stage: undefined, error: message };
}

/** One stream event applied to its turn (route, plan, pages and checks are not shown, as in luccy). */
export function applyEvent(turn: Turn, name: string, data: Record<string, unknown>, position: StreamPosition): Turn {
  const t: Turn = { ...turn, job: position.job || turn.job, lastEventId: position.lastEventId || turn.lastEventId };
  if (name === "stage") return t.pending && typeof data.stage === "string" ? { ...t, stage: data.stage } : t;
  if (name === "answer" || name === "done") {
    return {
      ...t, pending: false, stage: undefined, error: undefined,
      answer: {
        text: typeof data.answer === "string" ? data.answer : "",
        footer: typeof data.footer === "string" ? data.footer : "",
        citations: citationsOf(data.citations),
        gaps: gapsOf(data.gaps),
        log: answerIdOf(data.log) ?? t.answer?.log ?? null,
        checking: name === "answer",
        cut: name === "done" && data.cut === true,
        stale: staleWarning(data.answer_html) || undefined,
      },
    };
  }
  if (name === "error") {
    const message = typeof data.message === "string" ? data.message.trim() : "";
    return failTurn(t, message === "cancelled" ? CANCELLED : message || ANSWER_FAILED);
  }
  return t;
}

const turnId = () => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
const isOpen = (t: Turn) => t.pending || !!t.answer?.checking;
type Feedback = { verdict: FeedbackVerdict; note: string };

/** The analyzer chat: one thread per user, one question in flight. */
export function AnalyzerChat({ userId, status }: { userId: number; status: AnalyzerStatus }) {
  const [thread, setThread] = useState<Thread>(() => loadThread(userId));
  const [draft, setDraft] = useState("");
  const [panel, setPanel] = useState<PanelTarget | null>(null);
  // The turn whose progress the live region reads out.
  const [announced, setAnnounced] = useState<string | null>(null);
  const initial = useRef(thread);
  const threadRef = useRef(thread);
  // A thread taken from another tab, so it is not written straight back.
  const adopted = useRef<Thread | null>(null);
  const streamRef = useRef<{ id: string; controller: AbortController } | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const allSources: AnalyzerSource[] = ANALYZER_SOURCES.filter((s) => !status.sources?.length || status.sources.includes(s));
  const activeSources = allSources.filter((s) => thread.sources.includes(s));
  const busy = thread.turns.some(isOpen);
  const pendingTurn = thread.turns.find((t) => t.pending);

  useEffect(() => {
    threadRef.current = thread;
    if (thread !== adopted.current) saveThread(userId, thread);
  }, [userId, thread]);

  const patch = useCallback((id: string, change: (turn: Turn) => Turn) => {
    setThread((t) => ({ ...t, turns: t.turns.map((turn) => (turn.id === id ? change(turn) : turn)) }));
  }, []);

  const stop = useCallback(() => {
    streamRef.current?.controller.abort();
    streamRef.current = null;
  }, []);

  const run = useCallback((id: string, start: { ask?: AskBody; resume?: StreamPosition }) => {
    stop();
    const controller = new AbortController();
    const stream = { id, controller };
    streamRef.current = stream;
    setAnnounced(id);
    let attached = !!start.resume;
    followAnswer({
      ...start,
      signal: controller.signal,
      onEvent: (name, data, position) => {
        attached ||= !!position.job;
        patch(id, (turn) => applyEvent(turn, name, data, position));
      },
    })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        patch(id, (turn) => failTurn(turn, failureText(error, attached)));
      })
      .finally(() => { if (streamRef.current === stream) streamRef.current = null; });
  }, [patch, stop]);

  // An answer that was running when the page was left: re-attach to it, as
  // luccy's page does after a reload. Without a job id there is nothing to join.
  useEffect(() => {
    const open = initial.current.turns.filter(isOpen);
    open.forEach((turn, i) => {
      if (i === open.length - 1 && turn.job) run(turn.id, { resume: { job: turn.job, lastEventId: turn.lastEventId ?? "" } });
      else patch(turn.id, (t) => failTurn(t, INTERRUPTED));
    });
    return stop;
  }, [run, patch, stop]);

  // The same thread open in another tab: follow what it saves rather than
  // overwrite it with this tab's older copy. A new chat there ends ours too.
  useEffect(() => {
    const key = threadKey(userId);
    const onStorage = (event: StorageEvent) => {
      let ours = false;
      try { ours = event.storageArea === localStorage && (event.key === key || event.key === null); } catch { /* storage blocked */ }
      if (!ours) return;
      const incoming = parseThread(event.newValue);
      if (streamRef.current && incoming.conversation !== threadRef.current.conversation) {
        stop();
        setPanel(null);
      }
      const running = streamRef.current?.id ?? null;
      setThread((current) => {
        const next = syncThread(current, incoming, running);
        adopted.current = next === incoming ? next : null;
        return next;
      });
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [userId, stop]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  }, [thread.turns.length, pendingTurn?.id]);

  // The box grows with its text up to 200px, then scrolls.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  const openCitation = useCallback((citation: AnalyzerCitation, answerId: string | null) => setPanel({ citation, answerId }), []);
  const setFeedback = useCallback((id: string, feedback: Feedback) => patch(id, (t) => ({ ...t, feedback })), [patch]);

  function ask() {
    const question = draft.trim();
    if (!question || busy) return;
    // Sent only when narrowed, as luccy does; none left on (a stale saved choice) means all.
    const sources = activeSources.length && activeSources.length < allSources.length ? activeSources : undefined;
    const turn: Turn = { id: turnId(), question, sources, style: thread.style, started: Date.now(), pending: true, stage: "starting" };
    const history = historyOf(thread.turns);
    const body: AskBody = {
      question,
      conversation: thread.conversation,
      ...(sources ? { sources } : {}),
      ...(thread.style === "plain" ? { style: "plain" as const } : {}),
      ...(history.length ? { history } : {}),
    };
    setThread((t) => ({ ...t, turns: [...t.turns, turn].slice(-MAX_STORED_TURNS) }));
    setDraft("");
    run(turn.id, { ask: body });
  }

  function newChat() {
    stop();
    setPanel(null);
    setAnnounced(null);
    setThread((t) => ({ ...newThread(), sources: t.sources, style: t.style }));
    inputRef.current?.focus();
  }

  function toggleSource(source: AnalyzerSource) {
    setThread((t) => {
      const on = t.sources.includes(source);
      if (on && allSources.filter((s) => t.sources.includes(s)).length <= 1) return t;   // the last one stays on
      return { ...t, sources: ANALYZER_SOURCES.filter((s) => (s === source ? !on : t.sources.includes(s))) };
    });
  }

  // Read out each stage, then that the answer is ready or what went wrong.
  const watched = thread.turns.find((t) => t.id === announced);
  const liveText = !watched ? "" : watched.pending ? stageLabel(watched.stage) : watched.answer ? "Answer ready" : watched.error || ANSWER_FAILED;
  const lastSourceOn = activeSources.length <= 1;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="mb-3 flex justify-end">
        <Button variant="outline" size="sm" onClick={newChat}><Plus className="h-4 w-4" /> New chat</Button>
      </div>
      {status.healthy === false && (
        <p className="mb-3 rounded-lg border border-line bg-warn/10 p-3 text-xs text-foreground/80">
          The Document Analyzer isn&apos;t responding right now. Questions may fail; please try again in a few minutes.
        </p>
      )}

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-line bg-card">
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3 sm:p-5">
          {thread.turns.length === 0 && (
            <div className="mx-auto max-w-lg py-8 text-center">
              <div className="mx-auto mb-3 grid h-10 w-10 place-items-center rounded-xl bg-brand text-white"><FileSearch className="h-5 w-5" /></div>
              <h2 className="text-[15px] font-semibold">Ask about company knowledge</h2>
              <p className="mt-2 text-[13px] text-muted-foreground">
                It reads Confluence, Jira, GitHub, the dbt and Power BI definitions and Google Drive to answer questions about
                strategy, processes, metrics and past decisions — with citations. Click any citation to open the document and
                the passages it was cited for.
              </p>
            </div>
          )}
          {thread.turns.map((turn) => <TurnView key={turn.id} turn={turn} onOpen={openCitation} onFeedback={setFeedback} />)}
          <div ref={endRef} />
        </div>
        <div role="status" aria-live="polite" className="sr-only">{liveText}</div>

        <form
          className="border-t border-line p-3"
          onSubmit={(event) => { event.preventDefault(); ask(); }}
        >
          <textarea
            ref={inputRef}
            value={draft}
            rows={2}
            maxLength={10000}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                ask();
              }
            }}
            aria-label="Ask about company docs, metrics, decisions"
            placeholder={busy ? BUSY_PLACEHOLDER : PLACEHOLDER}
            className="block max-h-[200px] min-h-[44px] w-full resize-none rounded-lg border border-input bg-panel px-3 py-2 text-[13px] outline-none focus:ring-1 focus:ring-ring"
          />
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
            <div role="group" aria-label="Sources searched" className="flex flex-wrap items-center gap-1">
              {allSources.map((source) => {
                const on = activeSources.includes(source);
                // The last one stays focusable and says why it won't switch off.
                const last = on && lastSourceOn;
                return (
                  <button
                    key={source}
                    type="button"
                    aria-pressed={on}
                    aria-disabled={last || undefined}
                    aria-describedby={last ? "analyzer-last-source" : undefined}
                    title={last ? undefined : on ? `Stop searching ${SOURCE_LABELS[source]}` : `Search ${SOURCE_LABELS[source]}`}
                    onClick={() => toggleSource(source)}
                    className={cn(
                      "h-7 rounded-full border px-2.5 text-[11.5px] font-medium transition-colors",
                      on ? "border-brand/40 bg-brand/10 text-foreground" : "border-line text-faint line-through hover:text-muted-foreground",
                      last && "cursor-not-allowed",
                    )}
                  >
                    {SOURCE_LABELS[source]}
                  </button>
                );
              })}
              {lastSourceOn && <span id="analyzer-last-source" className="px-1 text-[11px] text-faint">{LAST_SOURCE_HINT}</span>}
            </div>
            <div role="group" aria-label="Answer style" className="inline-flex rounded-full border border-line p-0.5">
              {([["", "Standard"], ["plain", "Simple words"]] as const).map(([value, label]) => (
                <button
                  key={label}
                  type="button"
                  aria-pressed={thread.style === value}
                  title={value === "plain" ? "Short sentences and everyday words. Same facts." : "Full answers with all the detail"}
                  onClick={() => setThread((t) => ({ ...t, style: value }))}
                  className={cn(
                    "h-6 rounded-full px-2.5 text-[11.5px] font-medium transition-colors",
                    thread.style === value ? "bg-foreground/[0.08] text-foreground" : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
            <Button type="submit" variant="brand" size="sm" className="ml-auto" disabled={busy || !draft.trim()}>
              Ask <ArrowUp aria-hidden="true" />
            </Button>
          </div>
        </form>
      </div>
      <DocumentPanel target={panel} onClose={() => setPanel(null)} />
    </div>
  );
}

/** One question and its answer. Memoized: typing or another turn's events
 *  leave the rendered markdown of earlier answers alone. */
const TurnView = memo(function TurnView({ turn, onOpen, onFeedback }: {
  turn: Turn;
  onOpen: (citation: AnalyzerCitation, answerId: string | null) => void;
  onFeedback: (turnId: string, feedback: Feedback) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-tr-sm bg-brand px-3.5 py-2.5 text-[13px] leading-relaxed text-white shadow-card">
          {turn.question}
        </div>
      </div>
      <div className="flex items-start gap-2.5">
        <div className="hidden h-7 w-7 shrink-0 place-items-center rounded-lg bg-brand text-white sm:grid" aria-hidden="true">
          <FileSearch className="h-3.5 w-3.5" />
        </div>
        <div className={cn(
          "min-w-0 flex-1 rounded-2xl rounded-tl-sm border border-line bg-panel px-3.5 py-2.5 text-[13px] leading-relaxed text-foreground/90 shadow-card sm:flex-none sm:max-w-[85%]",
          turn.error && !turn.answer && "border-err/40 text-err",
        )}>
          {turn.pending ? (
            <StageLine stage={turn.stage} started={turn.started} />
          ) : turn.answer ? (
            <AnswerView
              turn={turn}
              answer={turn.answer}
              onOpen={(citation) => onOpen(citation, turn.answer?.log ?? null)}
              onFeedback={(feedback) => onFeedback(turn.id, feedback)}
            />
          ) : (
            <p>{turn.error || ANSWER_FAILED}</p>
          )}
        </div>
      </div>
    </div>
  );
});
