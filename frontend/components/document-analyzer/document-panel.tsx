"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronUp, ChevronDown, ExternalLink, FileText, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/misc";
import { ApiError } from "@/lib/api";
import { safeSourceUrl } from "@/lib/knowledge-api";
import { ACTIVE_EVIDENCE_ID, highlightAnalyzerDocument } from "@/lib/analyzer-document-highlight";
import {
  analyzerApi,
  citationSource,
  SOURCE_LABELS,
  type AnalyzerCitation,
  type AnalyzerDocument,
} from "@/lib/document-analyzer-api";

export interface PanelTarget { citation: AnalyzerCitation; answerId: string | null }

// The page body lives in a sandboxed frame with no scripts and no same-origin
// access; this reader and its CSP are all it gets. Links open nowhere (popups
// are blocked by the sandbox): "Open source" is the way to the original.
const FRAME_CSS = `
  :root { color-scheme: light; }
  body { margin: 0; padding: 18px 24px 60px; font: 16px/1.75 system-ui, -apple-system, "Segoe UI", sans-serif; color: #24221f; background: #fff; overflow-wrap: anywhere; }
  h1, h2, h3, h4, h5, h6 { line-height: 1.3; margin: 1.2em 0 .5em; }
  h1 { font-size: 1.35em; } h2 { font-size: 1.2em; } h3 { font-size: 1.08em; } h4, h5, h6 { font-size: 1em; }
  p, ul, ol, pre, table, blockquote { margin: 0 0 .8em; }
  a { color: #0f7a5a; }
  code, pre { font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: #f4f3f0; border-radius: 4px; }
  code { padding: 1px 4px; } pre { padding: 8px 10px; overflow-x: auto; } pre code { padding: 0; background: none; }
  table { border-collapse: collapse; display: block; overflow-x: auto; max-width: 100%; font-size: 14px; }
  th, td { border: 1px solid #e3e1dc; padding: 4px 8px; text-align: left; vertical-align: top; }
  th { background: #f4f3f0; }
  blockquote { border-left: 3px solid #e3e1dc; padding-left: 10px; color: #5f5b55; }
  img { max-width: 100%; height: auto; }
  hr { border: 0; border-top: 1px solid #e3e1dc; }
  mark.analyzer-evidence { background: #C9EBD9; color: inherit; border-radius: 5px; padding: 2px 3px; margin: 0 -1px; -webkit-box-decoration-break: clone; box-decoration-break: clone; }
  mark.analyzer-evidence-active { scroll-margin-block: 24px; animation: evidence-pulse 2.4s ease-out; }
  @keyframes evidence-pulse { from { box-shadow: 0 0 0 4px #79b99a80; } to { box-shadow: 0 0 0 0 transparent; } }
  @media (prefers-reduced-motion: reduce) { mark.analyzer-evidence-active { animation: none; } }
`;

/** The source frame's HTML under a no-network CSP. */
export function documentFrameHtml(bodyHtml: string): string {
  return '<!doctype html><html><head><meta charset="utf-8">' +
    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">" +
    '<meta name="referrer" content="no-referrer"><base target="_blank">' +
    `<style>${FRAME_CSS}</style></head><body>${bodyHtml}</body></html>`;
}

function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 404) return "This document isn't available any more. It may have been removed or not added yet.";
    if (error.status === 401) return "Your sign-in expired. Reload the page and sign in again.";
    return "The Document Analyzer had a problem opening this document. Please try again in a minute.";
  }
  return "The Document Analyzer can't be reached right now. Check your internet connection and try again.";
}

/** Docked source reader; closing it is explicit, and the chat stays usable beside it. */
export function DocumentPanel({ target, onClose }: { target: PanelTarget | null; onClose: () => void }) {
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => { if (target) panelRef.current?.focus(); }, [target]);
  if (!target) return null;
  return (
    <aside
      ref={panelRef}
      tabIndex={-1}
      aria-label="Source document"
      data-analyzer-document-panel
      onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); onClose(); } }}
      className="flex min-h-0 w-full flex-1 flex-col overflow-hidden rounded-lg border border-line bg-card outline-none lg:w-[42vw] lg:max-w-2xl lg:flex-none"
    >
      <DocumentView key={`${target.citation.id}|${target.answerId ?? ""}`} target={target} onClose={onClose} />
    </aside>
  );
}

function DocumentView({ target, onClose }: { target: PanelTarget; onClose: () => void }) {
  const { citation, answerId } = target;
  const query = useQuery({
    queryKey: ["document-analyzer", "document", citation.id, answerId],
    queryFn: () => analyzerApi.document(citation.id, answerId),
    retry: false,
    staleTime: 5 * 60_000,
  });
  const doc = query.data;
  const source = citationSource(citation);
  const kicker = doc?.source || (source ? SOURCE_LABELS[source] : "Document");
  const openUrl = safeSourceUrl(doc?.url ?? citation.url);
  const title = doc?.title || citation.title || "Document";

  return (
    <>
      <header className="shrink-0 space-y-2 border-b border-line bg-panel px-4 py-3 sm:px-5">
        <div className="flex items-center gap-2">
          <span className="inline-flex min-w-0 items-center gap-1.5 rounded-full bg-brand/10 px-2.5 py-1.5 text-[11.5px] font-semibold uppercase tracking-[0.05em] text-brand">
            <FileText className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{kicker}</span>
          </span>
          <span className="flex-1" />
          {openUrl && (
            <a href={openUrl} target="_blank" rel="noopener noreferrer"
              className="inline-flex shrink-0 items-center gap-1 rounded-full bg-brand px-3 py-2 text-xs font-semibold text-white hover:bg-brand/90">
              Open source <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
            </a>
          )}
          <Button size="icon" variant="ghost" className="h-8 w-8 shrink-0" aria-label="Close the document panel" onClick={onClose}><X /></Button>
        </div>
        {!!doc?.path?.length && <p className="truncate text-xs text-muted-foreground">{doc.path.join(" › ")}</p>}
        <h2 className="text-xl font-bold leading-tight sm:text-2xl">{title}</h2>
        {doc?.modified && <p className="text-xs text-muted-foreground">modified {doc.modified}</p>}
      </header>
      {query.isPending ? (
        <LoadingState label="Opening the document…" />
      ) : query.isError ? (
        <div role="alert" className="m-4 space-y-3 rounded-lg border border-line bg-panel p-4 text-[13px] sm:m-5">
          <p className="font-semibold">We couldn&apos;t open this document</p>
          <p className="text-muted-foreground">{errorMessage(query.error)}</p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => void query.refetch()} disabled={query.isFetching}>Try again</Button>
            {openUrl && (
              <Button size="sm" variant="ghost" asChild>
                <a href={openUrl} target="_blank" rel="noopener noreferrer">Open the source <ExternalLink aria-hidden="true" /></a>
              </Button>
            )}
          </div>
        </div>
      ) : doc ? (
        <DocumentBody doc={doc} title={title} />
      ) : null}
    </>
  );
}

function DocumentBody({ doc, title }: { doc: AnalyzerDocument; title: string }) {
  const passages = useMemo(() => Array.isArray(doc.passages) ? doc.passages.filter((p) => p && (p.text || p.heading)) : [], [doc.passages]);
  const [current, setCurrent] = useState(0);
  const choseInitialPassage = useRef(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const highlighted = useMemo(() => highlightAnalyzerDocument(doc.body_html || "<p><em>This document has no text.</em></p>", passages, current), [doc.body_html, passages, current]);
  const frameHtml = useMemo(() => documentFrameHtml(highlighted.html), [highlighted.html]);
  useEffect(() => {
    if (choseInitialPassage.current) return;
    choseInitialPassage.current = true;
    setCurrent(highlighted.matchedPassages[0] ?? 0);
  }, [highlighted.matchedPassages]);
  const count = passages.length;
  const step = (delta: number) => { if (count) setCurrent((i) => (i + delta + count) % count); };
  const matchStatus = highlighted.matchStatuses[current];
  const matchNote = matchStatus === "partial" ? "Matched part of the passage; the rest may have been rewritten since."
    : matchStatus === "heading" ? "We couldn't find the exact passage, so we show its section instead."
    : matchStatus === "missing" ? "This passage isn't in the current version of the document." : "";

  // Passage shortcuts apply inside this reader, leaving the chat's keyboard alone.
  useEffect(() => {
    if (count < 2) return;
    const panel = bodyRef.current?.closest<HTMLElement>("[data-analyzer-document-panel]");
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
      const target = event.target as HTMLElement | null;
      if (target?.isContentEditable || target?.closest?.("input, textarea, select")) return;
      const delta = event.key === "n" || event.key === "j" ? 1 : event.key === "p" || event.key === "k" ? -1 : 0;
      if (!delta) return;
      event.preventDefault();
      setCurrent((i) => (i + delta + count) % count);
    };
    panel?.addEventListener("keydown", onKey);
    return () => panel?.removeEventListener("keydown", onKey);
  }, [count]);

  return (
    <div ref={bodyRef} className="flex min-h-0 flex-1 flex-col">
      <section aria-label="Cited passages" className="shrink-0 border-b border-line px-4 py-3 sm:px-5">
        <div className="flex items-center gap-1.5">
          <Button size="icon" variant="ghost" className="h-8 w-8 rounded-full bg-panel" aria-label="Previous passage" disabled={count < 2} onClick={() => step(-1)}><ChevronUp /></Button>
          <Button size="icon" variant="ghost" className="h-8 w-8 rounded-full bg-panel" aria-label="Next passage" disabled={count < 2} onClick={() => step(1)}><ChevronDown /></Button>
          <span className="ml-auto text-xs font-semibold text-muted-foreground" aria-live="polite">
            {count ? `Passage ${current + 1} of ${count}` : "No cited passages"}
          </span>
        </div>
        {!!matchNote && <p className="mt-2 text-xs text-muted-foreground" role="status">{matchNote}</p>}
      </section>
      <div className="min-h-0 flex-1 p-3 sm:p-4">
        <DocumentFrame html={frameHtml} active={highlighted.activeMatched} title={`${title}: page text`} />
      </div>
    </div>
  );
}

/** Navigate to the fragment after layout so Chrome reliably scrolls the sandbox. */
function DocumentFrame({ html, active, title }: { html: string; active: boolean; title: string }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    const objectUrl = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [html, active]);
  return url ? (
    <iframe
      sandbox=""
      src={url}
      onLoad={() => {
        const fragment = `#${ACTIVE_EVIDENCE_ID}`;
        if (active && !url.endsWith(fragment)) {
          requestAnimationFrame(() => setUrl((current) => current === url ? url + fragment : current));
        }
      }}
      title={title}
      referrerPolicy="no-referrer"
      className="h-full min-h-[180px] w-full rounded-md border border-line bg-white"
    />
  ) : <LoadingState label="Preparing the document…" />;
}
