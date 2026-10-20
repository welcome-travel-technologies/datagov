"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { IntegrationContent } from "./integration-content";
import { Workflow, Database, CloudUpload, Bell, Bot, LayoutList, FileText, Settings2 } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Card, CardContent } from "@/components/ui/card";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { Button } from "@/components/ui/button";
import { SourceManager } from "@/components/knowledge/source-manager";
import { knowledgeApi } from "@/lib/knowledge-api";
import { useAuth } from "@/lib/auth";
import { sourceGroups } from "@/lib/integrations/source-groups";
import { DestinationCard } from "@/components/integrations/destination-card";
import { WorkflowPanel } from "@/components/integrations/workflow-panel";
import { BotTab, AlertsTab } from "@/components/integrations/notifications-panel";
import { isActiveStatus } from "@/components/integrations/status-badge";
import { SourcesOverview } from "@/components/integrations/overview/sources-overview";
import { resolveTab, type IntegrationsTab } from "@/components/integrations/overview/tabs";
import type { AdvancedView } from "@/lib/integrations-overview-api";
import { api, ApiError, getApiErrorMessage } from "@/lib/api";

const QK = ["integrations"];

export function IntegrationsManager() {
  // Unknown until the address is read, so an old ?tab= bookmark never loads the overview on the way.
  const [tab, setTab] = useState<IntegrationsTab | null>(null);
  const [view, setView] = useState<AdvancedView>("workflow");
  const [contentGroup, setContentGroup] = useState("");
  const apply = useCallback((value: string | null, group: string) => {
    const next = resolveTab(value);
    setTab(next.tab);
    if (next.view) setView(next.view);
    setContentGroup(group);
  }, []);
  useEffect(() => {
    const read = () => { const params = new URLSearchParams(window.location.search); apply(params.get("tab"), params.get("group") || ""); };
    read(); window.addEventListener("popstate", read); return () => window.removeEventListener("popstate", read);
  }, [apply]);
  // Advanced views keep their old `?tab=` names, so earlier bookmarks and links still open them.
  const navigate = (value: string, group = "") => { apply(value, group); const url = new URL(window.location.href); url.searchParams.set("tab", value); if (group) url.searchParams.set("group", group); else url.searchParams.delete("group"); window.history.pushState(null, "", url); };

  return (
    <Tabs value={tab ?? ""} onValueChange={(value) => navigate(value === "advanced" ? view : value)} className="w-full">
      <TabsList className="flex-wrap">
        <Trigger value="overview" icon={<LayoutList className="h-4 w-4" />} label="Overview" />
        <Trigger value="content" icon={<FileText className="h-4 w-4" />} label="Data & documents" />
        <Trigger value="advanced" icon={<Settings2 className="h-4 w-4" />} label="Advanced" />
      </TabsList>
      <TabsContent value="overview"><SourcesOverview onNavigate={navigate} /></TabsContent>
      <TabsContent value="content"><IntegrationContent key={contentGroup} initialGroup={contentGroup} /></TabsContent>
      <TabsContent value="advanced"><AdvancedSettings view={view} navigate={navigate} focusGroup={contentGroup} /></TabsContent>
    </Tabs>
  );
}

/** Every detailed setting, unchanged: workflow, sources, destinations, alerts and the bot.
 *  `focusGroup` (`?group=`, from the overview's Manage links) opens that source group. */
function AdvancedSettings({ view, navigate, focusGroup }: { view: AdvancedView; navigate: (value: string, group?: string) => void; focusGroup: string }) {
  const { user } = useAuth();
  const status = useQuery({ queryKey: ["knowledge", "status", user?.id], queryFn: knowledgeApi.status });
  const documentsEnabled = !!status.data?.enabled && !!status.data?.can_manage;
  const collections = useQuery({ queryKey: ["knowledge", "sources", user?.id], queryFn: knowledgeApi.sources, enabled: documentsEnabled });
  const groups = useQuery({ queryKey: ["knowledge", "groups", user?.id], queryFn: knowledgeApi.groups, enabled: documentsEnabled });
  const qc = useQueryClient();
  const onChanged = () => qc.invalidateQueries({ queryKey: QK });

  const query = useQuery({
    queryKey: QK,
    queryFn: api.integrations.getAll,
    refetchInterval: (q) => {
      const d = q.state.data;
      if (!d) return false;
      const active = [...d.sources, ...d.destinations].some((x) => isActiveStatus(x.last_run?.status));
      return active ? 4000 : false;
    },
  });

  if (query.isLoading) return <LoadingState label="Loading integrations…" />;
  if (query.isError || !query.data) {
    const forbidden = query.error instanceof ApiError && query.error.status === 403;
    return (
      <Card>
        <CardContent className="pt-6">
          <EmptyState
            title="Couldn't load integration settings"
            hint={forbidden ? "This area requires organization admin access." : getApiErrorMessage(query.error, "Please try again.")}
          />
          {!forbidden && <div className="flex justify-center pb-2"><Button variant="outline" size="sm" onClick={() => { void query.refetch(); }}>Try again</Button></div>}
        </CardContent>
      </Card>
    );
  }

  const { sources, destinations, hooks } = query.data;

  return (
    <Tabs value={view} onValueChange={(value) => navigate(value)} className="w-full">
      <TabsList className="flex-wrap">
        <Trigger value="workflow" icon={<Workflow className="h-4 w-4" />} label="Workflow" />
        <Trigger
          value="sources"
          icon={<Database className="h-4 w-4" />}
          label="Sources"
          count={sourceGroups(sources, groups.data?.results ?? []).length}
        />
        <Trigger
          value="destinations"
          icon={<CloudUpload className="h-4 w-4" />}
          label="Destinations"
          count={destinations.length}
        />
        <Trigger value="alerts" icon={<Bell className="h-4 w-4" />} label="Alerts" />
        <Trigger value="bot" icon={<Bot className="h-4 w-4" />} label="Bot" />
      </TabsList>

      <TabsContent value="workflow">
        <WorkflowPanel focusGroup={focusGroup || undefined} />
      </TabsContent>

      <TabsContent value="sources">
        {collections.isError && <p role="alert" className="mb-3 text-sm text-err">Document collections could not be loaded. Connector settings remain available.</p>}
        {collections.isLoading ? <LoadingState label="Loading sources…" /> : <SourceManager sources={collections.data?.results || []} connections={sources} configured={!!status.data?.configured} documentsEnabled={documentsEnabled} onBrowse={(group) => navigate("content", group)} onWorkflow={() => navigate("workflow")} focusGroup={focusGroup || undefined} />}
      </TabsContent>

      <TabsContent value="destinations">
        {destinations.length === 0 ? (
          <Card>
            <EmptyState title="No destinations configured" />
          </Card>
        ) : (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {destinations.map((d) => (
              <DestinationCard key={d.id} destination={d} onChanged={onChanged} />
            ))}
          </div>
        )}
      </TabsContent>

      <TabsContent value="alerts">
        <AlertsTab hooks={hooks} />
      </TabsContent>

      <TabsContent value="bot">
        <BotTab hooks={hooks} />
      </TabsContent>
    </Tabs>
  );
}

function Trigger({
  value,
  icon,
  label,
  count,
}: {
  value: string;
  icon: React.ReactNode;
  label: string;
  count?: number;
}) {
  return (
    <TabsTrigger value={value} className="inline-flex items-center gap-1.5">
      <span className="text-current opacity-70">{icon}</span>
      {label}
      {count !== undefined && (
        <span className="rounded-full bg-panel2 px-1.5 py-0.5 text-[11px] font-semibold text-faint">
          {count}
        </span>
      )}
    </TabsTrigger>
  );
}
