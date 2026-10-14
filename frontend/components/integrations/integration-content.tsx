"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ChevronLeft, ChevronRight, FileSearch, FileText, FolderInput, RefreshCw, Search, ShieldCheck, Upload } from "lucide-react";
import { getApiErrorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { documentHref, knowledgeApi } from "@/lib/knowledge-api";
import { useDebounced } from "@/lib/use-debounced";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { KnowledgeError, StatusBadge, displayDate } from "@/components/knowledge/shared";
import { useEvidenceReader } from "@/components/knowledge/evidence-workspace";
import { UploadDialog } from "@/components/knowledge/upload-dialog";

const PAGE_SIZE = 25;

export function IntegrationContent({ initialGroup = "" }: { initialGroup?: string }) {
  const { user } = useAuth();
  const reader = useEvidenceReader();
  const qc = useQueryClient();
  const [upload, setUpload] = useState(false);
  const [search, setSearch] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [groupId, setGroupId] = useState(initialGroup);
  useEffect(() => { setGroupId(initialGroup || new URLSearchParams(window.location.search).get("group") || ""); }, [initialGroup]);
  const [documentStatus, setDocumentStatus] = useState("");
  const [page, setPage] = useState(1);
  const [notice, setNotice] = useState("");
  const filter = useDebounced(search);
  const authorized = !!user && user.perms?.can_view_chat !== false;
  const status = useQuery({ queryKey: ["knowledge", "status", user?.id], queryFn: knowledgeApi.status, enabled: authorized, retry: false });
  const enabled = !!status.data?.enabled;
  const canManage = !!status.data?.can_manage;
  const sources = useQuery({ queryKey: ["knowledge", "sources", user?.id], queryFn: knowledgeApi.sources, enabled: authorized && enabled && canManage, retry: false });
  const documents = useQuery({ queryKey: ["knowledge", "documents", user?.id, filter, groupId, sourceId, documentStatus, page],
    queryFn: () => knowledgeApi.documents({ search: filter, source_id: sourceId, group: groupId, status: documentStatus, page, page_size: PAGE_SIZE }),
    enabled: authorized && enabled, retry: false, refetchInterval: 15_000, gcTime: 0, staleTime: 0, refetchOnWindowFocus: true });
  const groups = useQuery({ queryKey: ["knowledge", "groups", user?.id], queryFn: knowledgeApi.groups, enabled: authorized && enabled, gcTime: 0, staleTime: 0 });
  const allSources = sources.data?.results || [];
  const selectClass = "h-9 min-w-0 rounded-lg border border-input bg-panel px-3 text-xs outline-none focus:ring-1 focus:ring-ring";

  if (user && !authorized) return <EmptyState title="Data & documents access required" hint="Ask your organization administrator for AI Assistant access." icon={<ShieldCheck className="h-9 w-9" />} />;

  return <div className="space-y-5">
    <header className="relative overflow-hidden rounded-2xl border border-line bg-card p-5 sm:p-7">
      <div className="pointer-events-none absolute -right-12 -top-20 h-64 w-64 rounded-full bg-brand/[0.06] blur-2xl" />
      <div className="relative flex flex-wrap items-start justify-between gap-5">
        <div className="flex items-start gap-4">
          <div className="hidden rounded-xl bg-brand/10 p-3 text-brand sm:block"><FileSearch className="h-7 w-7" /></div>
          <div><p className="mb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-brand">Integration content</p>
            <h1 className="text-2xl font-semibold -tracking-[0.02em]">Data & documents</h1>
            <p className="mt-2 max-w-xl text-[13px] leading-relaxed text-muted-foreground">Find your documents, read the evidence, and ask questions with sources you can open.</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" asChild><Link href="/chat">Ask the assistant <ArrowRight /></Link></Button>
          {canManage && enabled && <Button variant="brand" disabled={!status.data?.configured} onClick={() => setUpload(true)}><Upload /> Upload documents</Button>}
        </div>
      </div>
      {enabled && <div className="relative mt-5 flex flex-wrap gap-x-6 gap-y-2 border-t border-line pt-4 text-xs text-muted-foreground">
        <span><strong className="font-semibold text-foreground">{documents.data?.count?.toLocaleString() ?? "—"}</strong> {filter || sourceId || documentStatus ? "matching " : ""}{documents.data?.count === 1 ? "document" : "documents"}</span>
        <span><strong className="font-semibold text-foreground">{groups.data?.results.length ?? "—"}</strong> sources</span>
        <span className="inline-flex items-center gap-1.5"><ShieldCheck className="h-3.5 w-3.5 text-brand" /> Only documents you can access</span>
      </div>}
    </header>

    {status.isPending && authorized && <LoadingState label="Loading Data & documents…" />}
    {status.isError && <KnowledgeError message={getApiErrorMessage(status.error, "Data & documents is unavailable.")} retry={() => { void status.refetch(); }} />}
    {status.data && !enabled && <div className="rounded-xl border border-line bg-card p-6"><EmptyState title="Data & documents is not enabled yet" hint="Your administrator can enable the document service when it is ready. Your catalog assistant remains available." icon={<FileSearch className="h-9 w-9" />} /></div>}
    {enabled && <>
      {!status.data?.configured && <div role="status" className="rounded-xl border border-warn/25 bg-warn/5 p-4 text-[13px]">The document service is not configured yet. {canManage ? "You can prepare sources while your administrator completes setup." : "Please contact your administrator."}</div>}
      {notice && <div role="status" className="flex items-center justify-between gap-3 rounded-xl border border-brand/20 bg-brand/5 p-4 text-[13px]"><span>{notice}</span><button className="text-xs text-muted-foreground hover:text-foreground" onClick={() => setNotice("")}>Dismiss</button></div>}
      <div className="space-y-4">
        <div className="flex justify-end"><Button variant="ghost" size="sm" onClick={() => { void qc.invalidateQueries({ queryKey: ["knowledge"] }); }}><RefreshCw /> Refresh</Button></div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="relative min-w-48 flex-1"><span className="sr-only">Search documents</span><Search className="absolute left-3 top-2.5 h-4 w-4 text-faint" /><Input value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} placeholder="Search document titles…" className="h-9 pl-9" /></label>
            <select aria-label="Filter by source" value={groupId} onChange={(event) => { setGroupId(event.target.value); setSourceId(""); setPage(1); }} className={selectClass}><option value="">All sources</option>{groups.data?.results.map((group) => <option key={group.id} value={group.id}>{group.name} ({group.document_count.toLocaleString()})</option>)}</select>
            {canManage && <select aria-label="Filter by collection" value={sourceId} onChange={(event) => { setSourceId(event.target.value); setPage(1); }} className={selectClass}><option value="">All collections</option>{allSources.filter((source) => !groupId || groups.data?.results.find((g) => g.id === groupId)?.collections.some((c) => c.id === source.id)).map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}</select>}
            <select aria-label="Filter by status" value={documentStatus} onChange={(event) => { setDocumentStatus(event.target.value); setPage(1); }} className={selectClass}><option value="">All statuses</option><option value="ready">Ready</option><option value="queued">Queued</option><option value="processing">Processing</option><option value="needs_ocr">Needs OCR</option><option value="failed">Failed</option></select>
          </div>
          {documents.isPending && <LoadingState label="Loading documents…" />}
          {documents.isError && <KnowledgeError message={getApiErrorMessage(documents.error, "Documents could not be loaded.")} retry={() => { void documents.refetch(); }} />}
          {documents.data && !documents.data.results.length && <div className="rounded-xl border border-line bg-card p-5"><EmptyState title={search || sourceId || documentStatus ? "No matching documents" : "Your document library starts here"}
            hint={search || sourceId || documentStatus ? "Try another title, source, or status." : canManage ? "Create a source, then upload a PDF or text file to make it searchable." : "Documents will appear here when an administrator adds a source you can access."}
            icon={<FolderInput className="h-9 w-9" />} />{canManage && !search && !sourceId && !documentStatus && <div className="mb-4 text-center"><Button variant="outline" asChild><Link href="/integrations?tab=sources">Manage sources <ArrowRight /></Link></Button></div>}</div>}
          {!!documents.data?.results.length && <div className="overflow-hidden rounded-xl border border-line bg-card shadow-card">
            <div className="hidden grid-cols-[minmax(0,1fr)_150px_120px_190px] gap-4 border-b border-line bg-panel px-5 py-3 text-[11px] font-semibold uppercase tracking-wider text-faint lg:grid"><span>Document</span><span>Source</span><span>Status</span><span>Updated</span></div>
            <div className="divide-y divide-line">{documents.data.results.map((doc) => <Link key={doc.id} href={documentHref({ document_id: doc.id, version_id: doc.current_version_id })}
              onClick={(event) => { if (reader && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); reader.open({ document_id: doc.id, version_id: doc.current_version_id || undefined }); } }}
              className="group grid items-center gap-3 px-4 py-4 transition-colors hover:bg-brand/[0.03] focus-visible:outline-brand sm:px-5 lg:grid-cols-[minmax(0,1fr)_150px_120px_190px] lg:gap-4">
              <div className="flex min-w-0 items-center gap-3"><div className="rounded-lg border border-line bg-panel p-2 text-brand"><FileText className="h-5 w-5" /></div><div className="min-w-0"><h2 className="truncate text-[13px] font-medium group-hover:text-brand">{doc.title}</h2><p className="mt-1 text-[11px] text-faint">{doc.mime_type === "application/pdf" ? "PDF document" : "Text document"}</p></div></div>
              <span className="truncate text-xs text-muted-foreground">{doc.source_name || doc.source_type}</span>
              <span><StatusBadge status={doc.status} /></span><span className="text-xs text-muted-foreground">{displayDate(doc.updated_at)}</span>
            </Link>)}</div>
          </div>}
          {!!documents.data?.count && <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground"><span>{(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, documents.data.count)} of {documents.data.count.toLocaleString()}</span><div className="flex items-center gap-2"><Button size="sm" variant="outline" disabled={page === 1} onClick={() => setPage((p) => p - 1)}><ChevronLeft /> Previous</Button><Button size="sm" variant="outline" disabled={page * PAGE_SIZE >= documents.data.count} onClick={() => setPage((p) => p + 1)}>Next <ChevronRight /></Button></div></div>}
      </div>
      <UploadDialog open={upload} onOpenChange={setUpload} sources={allSources} onUploaded={(unchanged) => { setNotice(unchanged ? "This document is already in the source. No duplicate was created." : "Document uploaded. Follow processing in Workflow."); }} />
    </>}
  </div>;
}
