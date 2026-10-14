"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { DocumentViewer } from "@/components/knowledge/document-viewer";
import { LoadingState } from "@/components/ui/misc";

function Reader() {
  const { id } = useParams<{ id: string }>();
  const search = useSearchParams();
  const version = search.get("version");
  const page = Number(search.get("page")) || 1;
  const queryChunk = search.get("chunk") || undefined;
  const [chunk, setChunk] = useState<string>();
  useEffect(() => {
    function readAnchor() {
      const hash = window.location.hash;
      try { setChunk(hash.startsWith("#chunk-") ? decodeURIComponent(hash.slice(7)) : queryChunk); }
      catch { setChunk(undefined); }
    }
    readAnchor();
    window.addEventListener("hashchange", readAnchor);
    return () => window.removeEventListener("hashchange", readAnchor);
  }, [queryChunk]);

  return <div className="mx-auto max-w-6xl">
    <Link href="/integrations?tab=content" className="mb-5 inline-flex items-center gap-2 text-[13px] text-muted-foreground hover:text-brand"><ArrowLeft className="h-4 w-4" /> Data & documents</Link>
    <DocumentViewer key={`${id}-${version}-${page}-${chunk}`} documentId={id} versionId={version} initialPage={page} chunkId={chunk} />
  </div>;
}

export default function DocumentPage() {
  return <Suspense fallback={<LoadingState label="Opening document…" />}><Reader /></Suspense>;
}
