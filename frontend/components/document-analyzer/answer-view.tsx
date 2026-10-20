"use client";

import { useEffect, useState } from "react";
import { Database, FileText, ThumbsDown, ThumbsUp } from "lucide-react";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  analyzerApi,
  citationSource,
  isOpenableDocumentId,
  isWarehouse,
  normalizeUrl,
  SOURCE_LABELS,
  STAGE_LABELS,
  withoutImages,
  type AnalyzerCitation,
  type FeedbackVerdict,
} from "@/lib/document-analyzer-api";
import type { Turn, TurnAnswer } from "./thread";

export const SIGNED_OUT = "Your sign-in has expired. Reload the page and sign in again, then send your question again.";
const VOTE_FAILED = "Your vote wasn't saved. Please click again.";

/** Luccy's words for a stage; an unknown stage shows its own name. */
export function stageLabel(stage?: string) {
  return (stage && STAGE_LABELS[stage]) || stage || STAGE_LABELS.starting;
}

/** "Searching the corpus… 12s" with luccy's three dots, while an answer runs.
 *  It keeps its own clock, so the second ticks re-render only this line. */
export function StageLine({ stage, started }: { stage?: string; started: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <span className="inline-flex items-center gap-2 text-muted-foreground">
      <span className="inline-flex gap-1" aria-hidden="true">
        {[0, 150, 300].map((delay) => (
          <span key={delay} className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand" style={{ animationDelay: `${delay}ms` }} />
        ))}
      </span>
      <span>{stageLabel(stage)}</span>
      <span className="tabular-nums text-faint">{Math.max(0, Math.round((now - started) / 1000))}s</span>
    </span>
  );
}

/** One analyzer answer: luccy's markdown, its source chips, what it could not
 *  cover, the settings it was asked with, the footer and the thumbs. */
export function AnswerView({ turn, answer, onOpen, onFeedback }: {
  turn: Turn;
  answer: TurnAnswer;
  onOpen: (citation: AnalyzerCitation) => void;
  onFeedback: (feedback: { verdict: FeedbackVerdict; note: string }) => void;
}) {
  const openable = answer.citations.filter((c) => isOpenableDocumentId(c.id));

  // A link in the prose that points at a cited page opens the panel, as in
  // luccy; Ctrl/Cmd/Shift/Alt-click keeps the browser's default (the source).
  function onAnswerClick(event: React.MouseEvent<HTMLDivElement>) {
    const link = (event.target as Element | null)?.closest?.("a[href]");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const href = normalizeUrl(link.getAttribute("href"));
    const citation = openable.find((c) => c.url && normalizeUrl(c.url) === href);
    if (!citation) return;
    event.preventDefault();
    onOpen(citation);
  }

  const used = [
    turn.sources ? `Sources: ${turn.sources.map((s) => SOURCE_LABELS[s]).join(", ")}` : "",
    turn.style === "plain" ? "Simple words" : "",
  ].filter(Boolean);

  return (
    <div className="space-y-3">
      <div onClick={onAnswerClick}>
        <Markdown className="[&_blockquote]:rounded-r-md [&_blockquote]:border-l-warn [&_blockquote]:bg-warn/10 [&_blockquote]:py-1.5 [&_blockquote]:text-foreground">
          {withoutImages(answer.text) || "_No answer text._"}
        </Markdown>
      </div>
      {answer.gaps.length > 0 && (
        <div className="text-[13px]">
          <p className="font-medium">Couldn&apos;t cover:</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-muted-foreground">
            {answer.gaps.map((gap, i) => <li key={i}>{gap}</li>)}
          </ul>
        </div>
      )}
      {answer.stale && <p className="text-[12px] text-warn">{answer.stale}</p>}
      {answer.citations.length > 0 && (
        <ul aria-label="Sources cited" className="flex flex-wrap gap-1.5">
          {answer.citations.map((c, i) => <li key={`${c.id}-${i}`} className="min-w-0 max-w-full"><CitationChip citation={c} onOpen={onOpen} /></li>)}
        </ul>
      )}
      {used.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {used.map((u) => <span key={u} className="rounded-full bg-foreground/[0.05] px-2 py-0.5 text-[11px] text-muted-foreground">{u}</span>)}
        </div>
      )}
      {answer.footer && <p className="text-[11px] text-faint">{answer.footer}</p>}
      {answer.checking && <p className="text-[11px] text-muted-foreground">Checking the numbers…</p>}
      {answer.cut && (
        <p className="text-[11px] text-warn">The number check didn&apos;t finish while you waited. Treat the numbers in this answer with care.</p>
      )}
      {answer.log && !answer.checking && <FeedbackBar answerId={answer.log} feedback={turn.feedback} onChange={onFeedback} />}
    </div>
  );
}

function CitationChip({ citation, onOpen }: { citation: AnalyzerCitation; onOpen: (citation: AnalyzerCitation) => void }) {
  const chip = "inline-flex max-w-full items-center gap-1.5 rounded-full border border-line bg-panel px-2.5 py-1 text-[12px]";
  if (isWarehouse(citation.id)) {
    return (
      <span className={cn(chip, "text-muted-foreground")} title="Live data: it cannot be opened as a document">
        <Database className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span className="truncate">{citation.title || "live data"}</span>
        {citation.title && <span className="shrink-0 text-[10.5px] text-faint">live data</span>}
      </span>
    );
  }
  const source = citationSource(citation);
  const label = citation.title || citation.id;
  if (!isOpenableDocumentId(citation.id)) {
    return <span className={cn(chip, "text-muted-foreground")}><FileText className="h-3.5 w-3.5 shrink-0" aria-hidden="true" /><span className="truncate">{label}</span></span>;
  }
  return (
    <button
      type="button"
      onClick={() => onOpen(citation)}
      title={`Open in the document viewer${source ? ` · ${SOURCE_LABELS[source]}` : ""}`}
      className={cn(chip, "text-foreground transition-colors hover:border-brand hover:text-brand focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring")}
    >
      <FileText className="h-3.5 w-3.5 shrink-0 text-brand" aria-hidden="true" />
      <span className="truncate">{label}</span>
    </button>
  );
}

/** Thumbs as in luccy: a thumb lights and opens an optional note; Send or Skip
 *  posts the verdict; clicking the lit thumb of a sent vote posts `cleared`. */
function FeedbackBar({ answerId, feedback, onChange }: {
  answerId: string;
  feedback?: { verdict: FeedbackVerdict; note: string };
  onChange: (feedback: { verdict: FeedbackVerdict; note: string }) => void;
}) {
  const [asking, setAsking] = useState<"right" | "wrong" | null>(null);
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sent = feedback?.verdict;
  const lit = asking || (sent !== "cleared" ? sent : null);

  async function send(verdict: FeedbackVerdict, text: string) {
    setAsking(null);
    setEditing(false);
    setError(null);
    setSending(true);
    try {
      await analyzerApi.feedback(answerId, verdict, text);
      onChange({ verdict, note: verdict === "cleared" ? "" : text });
    } catch (e) {
      setError(e instanceof ApiError && e.status === 401 ? SIGNED_OUT : VOTE_FAILED);
    } finally {
      setSending(false);
    }
  }

  function thumb(kind: "right" | "wrong") {
    setEditing(false);
    if (asking === kind) { setAsking(null); return; }
    if (!asking && sent === kind) { void send("cleared", ""); return; }
    setNote("");
    setError(null);
    setAsking(kind);
  }

  const thumbButton = (kind: "right" | "wrong", label: string, Icon: typeof ThumbsUp) => {
    const name = !asking && sent === kind ? `${label} (click again to undo)` : label;
    return (
      <Button type="button" variant="ghost" size="icon" className={cn("h-7 w-7", lit === kind && "bg-brand/15 text-brand")}
        aria-label={name} title={name} aria-pressed={lit === kind} disabled={sending} onClick={() => thumb(kind)}>
        <Icon />
      </Button>
    );
  };

  const prompt = asking === "right" ? "What was good?" : "What was wrong?";
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[12px]">
      {thumbButton("right", "Good answer", ThumbsUp)}
      {thumbButton("wrong", "Something is wrong", ThumbsDown)}
      {asking ? (
        <form className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5" onSubmit={(e) => { e.preventDefault(); void send(asking, note.trim().slice(0, 500)); }}>
          <input
            autoFocus
            value={note}
            maxLength={500}
            onChange={(e) => setNote(e.target.value)}
            aria-label={prompt}
            placeholder={`${prompt} (optional)`}
            className="h-7 min-w-0 flex-1 basis-40 rounded-md border border-input bg-panel px-2 text-[12px] outline-none focus:ring-1 focus:ring-ring"
          />
          <Button type="submit" size="sm" variant="brand" className="h-7 px-2.5">Send</Button>
          {editing
            ? <Button type="button" size="sm" variant="ghost" className="h-7 px-2.5" onClick={() => { setAsking(null); setEditing(false); }}>Cancel</Button>
            : <Button type="button" size="sm" variant="ghost" className="h-7 px-2.5" onClick={() => void send(asking, "")}>Skip</Button>}
        </form>
      ) : error ? (
        <span role="alert" className="text-err">{error}</span>
      ) : sent === "cleared" ? (
        <span className="text-muted-foreground">Vote removed</span>
      ) : sent ? (
        <span className="flex min-w-0 items-center gap-1.5 text-muted-foreground">
          <span className="min-w-0 break-words">Thanks, noted{feedback?.note ? `: ${feedback.note}` : ""}</span>
          <Button type="button" variant="link" size="sm" className="h-auto px-0 text-[12px]"
            onClick={() => { setAsking(sent); setEditing(true); setNote(feedback?.note ?? ""); }}>Edit</Button>
        </span>
      ) : null}
    </div>
  );
}
