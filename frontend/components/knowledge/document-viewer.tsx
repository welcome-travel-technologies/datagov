"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, ChevronLeft, ChevronRight, Copy, FileText, LockKeyhole, Upload } from "lucide-react";
import { ApiError, getApiErrorMessage } from "@/lib/api";
import { documentContentHref, documentHref, knowledgeApi, safeSourceUrl, type DocumentDetail } from "@/lib/knowledge-api";
import { Button } from "@/components/ui/button";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { KnowledgeError, StatusBadge, displayDate } from "./shared";
import { cn } from "@/lib/utils";
import { useAuth } from "@/lib/auth";
import { UploadDialog } from "./upload-dialog";
import { HighlightedText } from "./highlighted-text";

const PdfReader = dynamic(() => import("./pdf-reader"), { ssr: false, loading: () => <LoadingState label="Loading PDF viewer…" /> });

export interface DocumentViewerProps {
  documentId: string;
  versionId?: string | null;
  initialPage?: number | null;
  chunkId?: string;
  compact?: boolean;
}

export function DocumentViewer({ documentId, versionId, initialPage, chunkId, compact = false }: DocumentViewerProps) {
  const { user } = useAuth();
  const [version, setVersion] = useState(versionId || "");
  const doc = useQuery({
    queryKey: ["knowledge", "document", user?.id, documentId, version],
    queryFn: () => knowledgeApi.document(documentId, version),
    enabled: !!user,
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: (query) => ["queued", "processing"].includes(query.state.data?.status || "") ? 3000 : false,
    retry: false,
  });

  if (doc.isPending) return <LoadingState label="Opening document…" />;
  if (doc.isError) {
    const unavailable = doc.error instanceof ApiError && [403, 404].includes(doc.error.status);
    return <KnowledgeError message={unavailable
      ? "This document or version is unavailable, or you do not have access. Ask the document owner for access."
      : getApiErrorMessage(doc.error, "The document could not be opened.")} retry={unavailable ? undefined : () => { void doc.refetch(); }} />;
  }

  return <DocumentBody key={doc.data.version_id || documentId} document={doc.data} compact={compact}
    initialPage={initialPage} chunkId={chunkId} onVersion={setVersion} />;
}

function DocumentBody({ document: doc, compact, initialPage, chunkId, onVersion }: {
  document: DocumentDetail;
  compact: boolean;
  initialPage?: number | null;
  chunkId?: string;
  onVersion: (version: string) => void;
}) {
  const { user } = useAuth();
  const permissions = useQuery({ queryKey: ["knowledge", "status", user?.id], queryFn: knowledgeApi.status, enabled: !!user, retry: false });
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadNotice, setUploadNotice] = useState("");
  const citedChunk = doc.chunks.find((chunk) => chunk.id === chunkId);
  const pageNumbers = [...new Set([...doc.pages.map((p) => p.page), ...doc.chunks.map((c) => c.page).filter((p): p is number => !!p && p > 0)])].sort((a, b) => a - b);
  const [page, setPage] = useState(initialPage && initialPage > 0 ? initialPage : citedChunk?.page || pageNumbers[0] || 1);
  const isPdf = doc.mime_type === "application/pdf" && !!doc.content_url;
  const [view, setView] = useState<"pdf" | "text">(isPdf ? "pdf" : "text");
  const [copyStatus, setCopyStatus] = useState("");
  const versionId = doc.version_id || doc.current_version_id;
  const href = documentHref({ document_id: doc.id, version_id: versionId, page, chunk_id: chunkId });
  const contentHref = documentContentHref(doc.id, versionId);
  const originalSource = safeSourceUrl(doc.source_url);
  const pageText = doc.pages.find((p) => p.page === page)?.text;
  const pageChunks = doc.chunks.filter((chunk) => chunk.page === page || (chunk.page === null && page === 1));
  const [pdfPages, setPdfPages] = useState(0);
  const maxPage = pdfPages || pageNumbers[pageNumbers.length - 1];
  const versions = doc.versions.filter((v) => ["ready", "published", "completed", "succeeded"].includes(v.status) || v.id === versionId);

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(new URL(href, window.location.origin).href);
      setCopyStatus("Link copied");
    } catch {
      setCopyStatus("Could not copy. Open the full page to copy its address.");
    }
  }

  return (
    <article className="min-w-0 space-y-4">
      <header className="space-y-3">
        <div className="flex items-start gap-3">
          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-brand/10 text-brand"><FileText className="h-5 w-5" /></div>
          <div className="min-w-0 flex-1">
            <h2 className="break-words text-lg font-semibold leading-snug">{doc.title}</h2>
            <p className="mt-1 text-xs text-muted-foreground">{doc.source_name || doc.source_type || "Document"} · Updated {displayDate(doc.updated_at)}</p>
          </div>
          <StatusBadge status={doc.status} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={copyLink}><Copy /> Copy link</Button>
          {compact && <Button variant="outline" size="sm" asChild><Link href={href} target="_blank" rel="noopener noreferrer"><ArrowUpRight /> Open full page</Link></Button>}
          {isPdf && <Button variant="outline" size="sm" asChild><a href={`${contentHref}#page=${page}`} target="_blank" rel="noopener noreferrer"><ArrowUpRight /> Open PDF</a></Button>}
          {originalSource && <Button variant="outline" size="sm" asChild><a href={originalSource} target="_blank" rel="noopener noreferrer"><ArrowUpRight /> Open original source</a></Button>}
          {permissions.data?.can_manage && doc.source_type === "upload" && doc.mime_type === "application/pdf" && doc.source_id && <Button variant="outline" size="sm" disabled={!permissions.data.configured} onClick={() => setUploadOpen(true)}><Upload /> New version</Button>}
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"><LockKeyhole className="h-3 w-3" /> Access checked by DataGov</span>
        </div>
        {copyStatus && <p role="status" className="text-xs text-muted-foreground">{copyStatus}</p>}
        {uploadNotice && <p role="status" className="rounded-lg border border-brand/20 bg-brand/5 p-3 text-xs">{uploadNotice}</p>}
        {!!versions.length && (
          <label className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            Version
            <select aria-label="Document version" className="max-w-full rounded-md border border-input bg-panel px-2 py-1.5 text-foreground"
              value={versionId || ""} onChange={(event) => onVersion(event.target.value)}>
              {versions.map((v) => <option key={v.id} value={v.id}>{displayDate(v.created_at)}{v.id === doc.current_version_id ? " · Current" : " · Previous"}</option>)}
            </select>
            {versionId && versionId !== doc.current_version_id && <span className="text-warn">Viewing a previous version</span>}
          </label>
        )}
      </header>

      {doc.error && <KnowledgeError message={doc.error} />}
      {doc.status === "needs_ocr" && <p role="status" className="rounded-lg border border-warn/25 bg-warn/5 p-3 text-[13px]">This document needs OCR before its scanned pages can be searched. You can read the original PDF below.</p>}

      {citedChunk && <section id={`chunk-${chunkId}`} className="rounded-xl border border-brand/25 bg-brand/5 p-4">
        <h3 className="mb-2 text-xs font-semibold text-brand">Cited passage{citedChunk.page ? ` · Page ${citedChunk.page}` : ""}</h3>
        <p className="max-h-52 overflow-y-auto whitespace-pre-wrap break-words text-[13px] leading-relaxed">{citedChunk.text}</p>
      </section>}

      <div className="overflow-hidden rounded-xl border border-line bg-card">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-panel px-3 py-2">
          <div className="flex gap-1" role="group" aria-label="Document view">
            {isPdf && <Button size="sm" variant={view === "pdf" ? "secondary" : "ghost"} aria-pressed={view === "pdf"} onClick={() => setView("pdf")}>PDF</Button>}
            <Button size="sm" variant={view === "text" ? "secondary" : "ghost"} aria-pressed={view === "text"} onClick={() => setView("text")}>Extracted text</Button>
          </div>
          <div className="flex items-center gap-1">
            <Button size="icon" variant="ghost" aria-label="Previous page" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}><ChevronLeft /></Button>
            <label className="flex items-center gap-2 text-xs">Page
              <input aria-label="Page number" type="number" min={1} max={maxPage} value={page}
                className="w-16 rounded-md border border-input bg-card px-2 py-1.5 text-center"
                onChange={(event) => { const next = Number(event.target.value); if (Number.isInteger(next) && next > 0 && (!maxPage || next <= maxPage)) setPage(next); }} />
              {maxPage ? <span className="text-muted-foreground">of {maxPage}</span> : null}
            </label>
            <Button size="icon" variant="ghost" aria-label="Next page" disabled={!isPdf && !maxPage || !!maxPage && page >= maxPage} onClick={() => setPage((p) => p + 1)}><ChevronRight /></Button>
          </div>
        </div>
        {view === "pdf" && isPdf ? <div>
          <PdfReader key={`${versionId}-${page}`} url={contentHref} page={page} passage={citedChunk?.page === page ? citedChunk.text : undefined} onPages={setPdfPages} />
          <p className="border-t border-line px-4 py-2 text-xs text-muted-foreground">If the preview is unavailable, use Open PDF or switch to Extracted text.</p>
        </div> : <div className={cn("p-5 sm:p-8", compact ? "min-h-[280px]" : "min-h-[420px]")}>
          {pageText ? <HighlightedText text={pageText} passage={citedChunk?.page === page ? citedChunk.text : undefined} />
            : pageChunks.length ? <div className="mx-auto max-w-3xl space-y-5">{pageChunks.map((chunk) => <section key={chunk.id} className={cn(chunk.id === chunkId && "rounded-lg bg-brand/10 p-3")}>
              {chunk.section && <h3 className="mb-2 font-medium">{chunk.section}</h3>}
              <p className="whitespace-pre-wrap break-words text-[14px] leading-7">{chunk.text}</p>
            </section>)}</div>
              : <EmptyState title="No extracted text for this page" hint={isPdf ? "You can still read the original PDF. Scanned pages may need OCR before they are searchable." : "Text will appear after this document is successfully processed."} icon={<FileText className="h-8 w-8" />} />}
        </div>}
      </div>
      {doc.source_id && <UploadDialog open={uploadOpen} onOpenChange={setUploadOpen} sources={[]}
        replacement={{ documentId: doc.id, sourceId: doc.source_id, title: doc.title }}
        onUploaded={(unchanged) => setUploadNotice(unchanged ? "This file already exists as a version of this document." : "New version uploaded. Follow processing in Document Explorer; the published version stays available.")} />}
    </article>
  );
}
