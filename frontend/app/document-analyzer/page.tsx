"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { MessageSquare, Scale } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { AnalyzerChat } from "@/components/document-analyzer/analyzer-chat";
import { ComparisonView } from "@/components/document-analyzer/comparison-view";
import { isRunActive } from "@/components/document-analyzer/comparison-format";
import { analyzerApi, isComparisonHidden } from "@/lib/document-analyzer-api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

type View = "chat" | "compare";

const readView = (): View => (new URLSearchParams(window.location.search).get("view") === "compare" ? "compare" : "chat");

export default function DocumentAnalyzerPage() {
  const { user } = useAuth();
  // The status endpoint decides, for every signed-in user, and says why not in
  // plain words; can_view_document_analyzer only shows the nav item.
  const status = useQuery({
    queryKey: ["document-analyzer", "status", user?.id],
    queryFn: analyzerApi.status,
    enabled: !!user,
    retry: false,
    staleTime: 30_000,
  });

  // Comparison runs are for organization admins (perms.is_admin, as elsewhere);
  // a 403 or 404 from the backend hides the view rather than showing an error.
  const isAdmin = !!user?.perms?.is_admin;
  const comparisons = useQuery({
    queryKey: ["document-analyzer", "comparisons", user?.id],
    queryFn: analyzerApi.comparisons,
    enabled: isAdmin,
    retry: false,
    staleTime: 30_000,
    refetchInterval: (query) => (query.state.data?.some((run) => isRunActive(run)) ? 15_000 : false),
  });
  const canCompare = isAdmin && !isComparisonHidden(comparisons.error);

  // ?view=compare opens the comparison; Back and Forward follow it.
  const [view, setView] = useState<View>("chat");
  useEffect(() => {
    const read = () => setView(readView());
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  function navigate(next: View) {
    setView(next);
    const url = new URL(window.location.href);
    if (next === "compare") url.searchParams.set("view", "compare");
    else url.searchParams.delete("view");
    window.history.pushState(null, "", url);
  }
  const comparing = canCompare && view === "compare";

  // Known status first: a failed background refetch (a moment offline) keeps
  // its data and must not tear down a running answer.
  let body: React.ReactNode;
  if (!user) body = <LoadingState />;
  else if (status.data) {
    body = status.data.available
      ? <AnalyzerChat key={user.id} userId={user.id} status={status.data} />
      : <Unavailable title="Document Analyzer unavailable" hint={status.data.reason || "The Document Analyzer isn't available for your account."} />;
  } else if (status.isError) {
    body = (
      <Unavailable title="Document Analyzer unavailable" hint="Its status could not be loaded. Please try again in a minute.">
        <Button size="sm" variant="outline" onClick={() => void status.refetch()} disabled={status.isFetching}>Try again</Button>
      </Unavailable>
    );
  } else body = <LoadingState label="Checking the Document Analyzer…" />;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Document Analyzer"
        description={comparing
          ? "How DataGov's answers compare with Luccy's on the same questions."
          : "Luccy's original answer engine, running inside DataGov — same retrieval, checks and answers as the standalone app."}
      />
      {canCompare && (
        <div role="group" aria-label="Page view" className="mb-3 inline-flex self-start rounded-lg border border-line bg-card p-1">
          <Button size="sm" variant={comparing ? "ghost" : "secondary"} aria-pressed={!comparing} onClick={() => navigate("chat")}><MessageSquare /> Chat</Button>
          <Button size="sm" variant={comparing ? "secondary" : "ghost"} aria-pressed={comparing} onClick={() => navigate("compare")}><Scale /> Compare with Luccy</Button>
        </div>
      )}
      {/* The chat stays mounted while the comparison shows, so a running answer carries on. */}
      <div hidden={comparing} className={cn("flex min-h-0 flex-1 flex-col", comparing && "hidden")}>{body}</div>
      {comparing && (
        <ComparisonView
          runs={comparisons.data}
          pending={comparisons.isPending}
          error={comparisons.error}
          onRetry={() => { void comparisons.refetch(); }}
        />
      )}
    </div>
  );
}

function Unavailable({ title, hint, children }: { title: string; hint: string; children?: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <EmptyState title={title} hint={hint} />
        {children && <div className="-mt-6 flex justify-center pb-2">{children}</div>}
      </CardContent>
    </Card>
  );
}
