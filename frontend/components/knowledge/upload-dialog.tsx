"use client";

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileText, Upload } from "lucide-react";
import { getApiErrorMessage } from "@/lib/api";
import { knowledgeApi, type KnowledgeSource } from "@/lib/knowledge-api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { KnowledgeError } from "./shared";

type Replacement = { documentId: string; sourceId: string; title: string };

export function UploadDialog({ open, onOpenChange, sources, onUploaded, replacement }: {
  open: boolean; onOpenChange: (open: boolean) => void; sources: KnowledgeSource[]; onUploaded: (unchanged: boolean) => void; replacement?: Replacement;
}) {
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="max-w-lg">
    <DialogHeader><DialogTitle>{replacement ? "Upload a new version" : "Upload documents"}</DialogTitle><DialogDescription>{replacement ? `Replace ${replacement.title}. The published version stays available until processing succeeds.` : "Your document will be processed and made searchable for the people with access to its source."}</DialogDescription></DialogHeader>
    {open && <UploadForm sources={sources} replacement={replacement} done={(unchanged) => { onUploaded(unchanged); onOpenChange(false); }} />}
  </DialogContent></Dialog>;
}

function UploadForm({ sources, done, replacement }: { sources: KnowledgeSource[]; done: (unchanged: boolean) => void; replacement?: Replacement }) {
  const available = sources.filter((source) => source.kind === "upload" && source.is_active && !source.pipeline_enabled && !source.refresh_paused);
  const qc = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [sourceId, setSourceId] = useState(replacement?.sourceId || available[0]?.id || "");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const upload = useMutation({
    mutationFn: () => knowledgeApi.upload(file!, sourceId, replacement?.documentId),
    onSuccess: (result) => { void qc.invalidateQueries({ queryKey: ["knowledge"] }); done(!!result.unchanged); },
  });
  function select(file?: File) {
    setError("");
    if (!file) return;
    if (!/\.(pdf|txt|md)$/i.test(file.name)) { setError("Choose a PDF, TXT or Markdown file."); return; }
    if (!file.size) { setError("This file is empty. Choose a file with content."); return; }
    setFile(file);
  }

  return <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); if (file && sourceId) upload.mutate(); }}>
    {!available.length && !replacement ? <div className="rounded-lg border border-line bg-panel p-4 text-[13px]">Create an active upload collection in Sources before adding documents.</div> : <>
      {!replacement && <label className="block space-y-1.5 text-[13px] font-medium">Source
        <select value={sourceId} onChange={(e) => setSourceId(e.target.value)} className="h-9 w-full rounded-lg border border-input bg-panel px-3 text-[13px]">
          {available.map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}
        </select>
      </label>}
      <button type="button" onClick={() => fileInput.current?.click()} disabled={upload.isPending}
        onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)}
        onDrop={(event) => { event.preventDefault(); setDragging(false); if (!upload.isPending) select(event.dataTransfer.files[0]); }}
        className={`flex min-h-44 w-full flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed p-6 text-center transition-colors ${dragging ? "border-brand bg-brand/10" : "border-line-strong bg-panel hover:border-brand/50"}`}>
        {file ? <FileText className="h-8 w-8 text-brand" /> : <Upload className="h-8 w-8 text-brand" />}
        <span className="max-w-full break-words text-[13px] font-medium">{file ? file.name : "Drop a PDF, TXT or Markdown file here, or choose a file"}</span>
        <span className="text-xs text-muted-foreground">{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB · Click to replace` : "The original file stays in private storage."}</span>
      </button>
      <input ref={fileInput} type="file" accept="application/pdf,text/plain,text/markdown,.pdf,.txt,.md" className="sr-only" aria-label="Document file" onChange={(event) => select(event.target.files?.[0])} disabled={upload.isPending} />
      <p className="text-xs text-muted-foreground">Scanned PDFs without readable text may require OCR before they can be searched.</p>
      <Button variant="brand" type="submit" className="w-full" disabled={!file || !sourceId || upload.isPending}><Upload />{upload.isPending ? "Uploading…" : "Upload and process"}</Button>
    </>}
    {error && <KnowledgeError message={error} />}
    {upload.isError && <KnowledgeError message={getApiErrorMessage(upload.error, "The document could not be uploaded.")} />}
  </form>;
}
