"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { documentHref, type DocumentCitation } from "@/lib/knowledge-api";
import { DocumentViewer } from "./document-viewer";

type Selection = Pick<DocumentCitation, "document_id"> & Partial<DocumentCitation>;
const EvidenceContext = createContext<{ open: (citation: Selection) => void; isOpen: boolean } | null>(null);
export const useEvidenceReader = () => useContext(EvidenceContext);

/** Shared by all DataGov pages. No modal overlay or focus trap over the work. */
export function EvidenceWorkspace({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const container = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [width, setWidth] = useState(48);
  useEffect(() => {
    function resized() { if (selected && window.innerWidth < 1280) { router.push(documentHref(selected)); setSelected(null); } }
    window.addEventListener("resize", resized);
    return () => window.removeEventListener("resize", resized);
  }, [selected, router]);
  function open(citation: Selection) {
    if (window.innerWidth < 1280) {
      router.push(documentHref(citation));
      return;
    }
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSelected(citation);
    requestAnimationFrame(() => closeButton.current?.focus());
  }
  function close() {
    setSelected(null);
    returnFocus.current?.focus();
  }
  const resize = (value: number) => setWidth(Math.min(65, Math.max(32, value)));
  return <EvidenceContext.Provider value={{ open, isOpen: !!selected }}>
    <div ref={container} className="flex min-h-0 min-w-0 flex-1">
      {children}
      {selected && <>
        <div role="separator" aria-label="Resize evidence reader" aria-orientation="vertical" aria-valuemin={32} aria-valuemax={65} aria-valuenow={width}
          tabIndex={0} className="z-10 w-1.5 shrink-0 cursor-col-resize bg-line hover:bg-brand focus-visible:bg-brand focus-visible:outline-none"
          onKeyDown={(event) => { if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); resize(width + (event.key === "ArrowLeft" ? 3 : -3)); } }}
          onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); }}
          onPointerMove={(event) => { if (!event.currentTarget.hasPointerCapture(event.pointerId)) return; const box = container.current?.getBoundingClientRect(); if (box) resize((box.right - event.clientX) / box.width * 100); }}
          onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)} />
        <aside aria-label="Evidence reader" style={{ flexBasis: `${width}%` }} className="flex min-w-0 shrink-0 flex-col bg-card" onKeyDown={(event) => { if (event.key === "Escape") close(); }}>
          <div className="flex items-center justify-between border-b border-line px-5 py-3">
            <div><h2 className="text-sm font-semibold">Source evidence</h2><p className="mt-1 text-xs text-muted-foreground">Read and keep working alongside it.</p></div>
            <Button ref={closeButton} size="icon" variant="ghost" aria-label="Close evidence reader" onClick={close}><X /></Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-5">
            <DocumentViewer key={`${selected.document_id}-${selected.version_id}-${selected.chunk_id}-${selected.page}`}
              documentId={selected.document_id} versionId={selected.version_id} initialPage={selected.page} chunkId={selected.chunk_id} compact />
          </div>
        </aside>
      </>}
    </div>
  </EvidenceContext.Provider>;
}
