"use client";

import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { AnalyzerChat } from "@/components/document-analyzer/analyzer-chat";
import { analyzerApi } from "@/lib/document-analyzer-api";
import { useAuth } from "@/lib/auth";

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
        title="Luccy (Beta)"
        description="Ask questions about your company’s documents."
      />
      {body}
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
