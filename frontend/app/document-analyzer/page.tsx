"use client";

import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { analyzerApi } from "@/lib/document-analyzer-api";
import { useAuth } from "@/lib/auth";

/** luccy's own chat page, exactly as the standalone app shows it (its look, chats and
 *  history, sources and feedback), served through DataGov's sign-in (backend page_gate). */
const LUCCY_PAGE = "/api/document-analyzer/luccy/";

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

  // Known status first: a failed background refetch must not unload luccy's page.
  if (user && status.data?.available) {
    return <iframe src={LUCCY_PAGE} title="Luccy" className="h-full w-full flex-1 border-0" />;
  }

  let body: React.ReactNode;
  if (!user) body = <LoadingState />;
  else if (status.data) {
    body = <Unavailable title="Luccy unavailable" hint={status.data.reason || "Luccy isn't available for your account."} />;
  } else if (status.isError) {
    body = (
      <Unavailable title="Luccy unavailable" hint="Its status could not be loaded. Please try again in a minute.">
        <Button size="sm" variant="outline" onClick={() => void status.refetch()} disabled={status.isFetching}>Try again</Button>
      </Unavailable>
    );
  } else body = <LoadingState label="Checking Luccy…" />;

  return (
    <div className="flex h-full min-h-0 flex-col px-4 py-5 sm:px-7 sm:py-7">
      <PageHeader title="Luccy (Beta)" description="Ask questions about your company’s documents." />
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
