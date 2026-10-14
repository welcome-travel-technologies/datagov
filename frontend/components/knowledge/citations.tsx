"use client";

import Link from "next/link";
import { ArrowUpRight, FileText } from "lucide-react";
import { documentHref, type DocumentCitation } from "@/lib/knowledge-api";
import { useEvidenceReader } from "./evidence-workspace";

export function DocumentCitations({ citations }: { citations: DocumentCitation[] }) {
  const reader = useEvidenceReader();
  const valid = citations.filter((citation) => citation.document_id && citation.version_id);
  if (!valid.length) return null;

  return (
    <section className="mt-4 border-t border-line pt-3" aria-label="Answer sources">
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Sources · {valid.length}</h3>
      <div className="grid gap-2 sm:grid-cols-2">
        {valid.map((citation, index) => <div key={`${citation.document_id}-${citation.version_id}-${citation.chunk_id || index}`}
          className="group flex min-w-0 items-start gap-2 rounded-lg border border-line bg-card p-2.5 transition-colors hover:border-brand/40">
          <button type="button" onClick={() => reader ? reader.open(citation) : window.location.assign(documentHref(citation))} aria-label={`Preview source ${index + 1}: ${citation.title}`}
            className="flex min-w-0 flex-1 items-start gap-2 text-left focus-visible:outline-brand">
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-md bg-brand/10 text-[11px] font-semibold text-brand">{citation.citation_id?.startsWith("E") ? citation.citation_id : index + 1}</span>
            <span className="min-w-0">
              <span className="line-clamp-2 break-words text-xs font-medium">{citation.title || "Document"}</span>
              <span className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground"><FileText className="h-3 w-3" />{citation.page ? `Page ${citation.page}` : "Document"}{citation.source_type ? ` · ${citation.source_type}` : ""}</span>
              {citation.excerpt && <span className="mt-1.5 line-clamp-2 break-words text-[11px] text-muted-foreground">{citation.excerpt}</span>}
            </span>
          </button>
          <Link href={documentHref(citation)} target="_blank" rel="noopener noreferrer" className="shrink-0 rounded p-1 text-muted-foreground hover:text-brand" aria-label={`Open ${citation.title} in Document Explorer`}><ArrowUpRight className="h-3.5 w-3.5" /></Link>
        </div>)}
      </div>
    </section>
  );
}
