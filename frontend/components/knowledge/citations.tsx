"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowUpRight, FileText } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { documentHref, type DocumentCitation } from "@/lib/knowledge-api";
import { DocumentViewer } from "./document-viewer";

export function DocumentCitations({ citations }: { citations: DocumentCitation[] }) {
  const [selected, setSelected] = useState<DocumentCitation | null>(null);
  const valid = citations.filter((citation) => citation.document_id && citation.version_id);
  if (!valid.length) return null;

  return (
    <section className="mt-4 border-t border-line pt-3" aria-label="Answer sources">
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Sources · {valid.length}</h3>
      <div className="grid gap-2 sm:grid-cols-2">
        {valid.map((citation, index) => <div key={`${citation.document_id}-${citation.version_id}-${citation.chunk_id || index}`}
          className="group flex min-w-0 items-start gap-2 rounded-lg border border-line bg-card p-2.5 transition-colors hover:border-brand/40">
          <button type="button" onClick={() => setSelected(citation)} aria-label={`Preview source ${index + 1}: ${citation.title}`}
            className="flex min-w-0 flex-1 items-start gap-2 text-left focus-visible:outline-brand">
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-md bg-brand/10 text-[11px] font-semibold text-brand">{index + 1}</span>
            <span className="min-w-0">
              <span className="line-clamp-2 break-words text-xs font-medium">{citation.title || "Document"}</span>
              <span className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground"><FileText className="h-3 w-3" />{citation.page ? `Page ${citation.page}` : "Document"}{citation.source_type ? ` · ${citation.source_type}` : ""}</span>
              {citation.excerpt && <span className="mt-1.5 line-clamp-2 break-words text-[11px] text-muted-foreground">{citation.excerpt}</span>}
            </span>
          </button>
          <Link href={documentHref(citation)} target="_blank" rel="noopener noreferrer" className="shrink-0 rounded p-1 text-muted-foreground hover:text-brand" aria-label={`Open ${citation.title} in Document Explorer`}><ArrowUpRight className="h-3.5 w-3.5" /></Link>
        </div>)}
      </div>
      <Dialog open={!!selected} onOpenChange={(open) => { if (!open) setSelected(null); }}>
        <DialogContent className="inset-y-0 left-auto right-0 flex h-dvh max-h-dvh w-full max-w-3xl translate-x-0 translate-y-0 flex-col gap-0 rounded-none p-0">
          <div className="border-b border-line px-5 py-4 pr-12">
            <DialogTitle className="text-[14px]">Document Explorer</DialogTitle>
            <DialogDescription className="mt-1">Read the source behind this answer.</DialogDescription>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
            {selected && <DocumentViewer key={`${selected.document_id}-${selected.version_id}-${selected.chunk_id || selected.page}`} documentId={selected.document_id}
              versionId={selected.version_id} initialPage={selected.page} chunkId={selected.chunk_id} compact />}
          </div>
        </DialogContent>
      </Dialog>
    </section>
  );
}
