"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { IntegrationContent } from "./integration-content";
import { Workflow, Database, CloudUpload, Bell, Bot } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Card, CardContent } from "@/components/ui/card";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SourceManager } from "@/components/knowledge/source-manager";
import { knowledgeApi } from "@/lib/knowledge-api";
import { useAuth } from "@/lib/auth";
import { sourceGroups } from "@/lib/integrations/source-groups";
import { DestinationCard } from "@/components/integrations/destination-card";
import { WorkflowPanel } from "@/components/integrations/workflow-panel";
import { BotTab, AlertsTab } from "@/components/integrations/notifications-panel";
import { isActiveStatus } from "@/components/integrations/status-badge";
import { api } from "@/lib/api";

const QK = ["integrations"];

export function IntegrationsManager() {
  const [tab, setTab] = useState("workflow");
  const [contentGroup, setContentGroup] = useState("");
  useEffect(() => {
    const read = () => { const params = new URLSearchParams(window.location.search); const value = params.get("tab"); setTab(value && ["workflow", "content", "sources", "destinations", "alerts", "bot"].includes(value) ? value : "workflow"); setContentGroup(params.get("group") || ""); };
    read(); window.addEventListener("popstate", read); return () => window.removeEventListener("popstate", read);
  }, []);
  const navigate = (value: string, group = "") => { setTab(value); setContentGroup(group); const url = new URL(window.location.href); url.searchParams.set("tab", value); if (group) url.searchParams.set("group", group); else url.searchParams.delete("group"); window.history.pushState(null, "", url); };
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
    return (
      <Card>
        <CardContent className="pt-6">
          <EmptyState
            title="Couldn't load integrations"
            hint="This area requires organization admin access."
          />
        </CardContent>
      </Card>
    );
  }

  const { sources, destinations, hooks } = query.data;

  return (
    <Tabs value={tab} onValueChange={(value) => navigate(value)} className="w-full">
      <TabsList className="flex-wrap">
        <Trigger value="workflow" icon={<Workflow className="h-4 w-4" />} label="Workflow" />
        <Trigger
          value="sources"
          icon={<Database className="h-4 w-4" />}
          label="Sources"
          count={sourceGroups(sources, groups.data?.results ?? []).length}
        />
        <Trigger value="content" icon={<Database className="h-4 w-4" />} label="Data & documents" />
        <Trigger
          value="destinations"
          icon={<CloudUpload className="h-4 w-4" />}
          label="Destinations"
          count={destinations.length}
        />
        <Trigger value="alerts" icon={<Bell className="h-4 w-4" />} label="Alerts" />
        <Trigger value="bot" icon={<Bot className="h-4 w-4" />} label="Bot" />
      </TabsList>
      <TabsContent value="content"><IntegrationContent key={contentGroup} initialGroup={contentGroup} /></TabsContent>

      <TabsContent value="workflow">
        <WorkflowPanel />
      </TabsContent>

      <TabsContent value="sources">
        {collections.isError && <p role="alert" className="mb-3 text-sm text-err">Document collections could not be loaded. Connector settings remain available.</p>}
        {collections.isLoading ? <LoadingState label="Loading sources…" /> : <SourceManager sources={collections.data?.results || []} connections={sources} configured={!!status.data?.configured} documentsEnabled={documentsEnabled} onBrowse={(group) => navigate("content", group)} onWorkflow={() => navigate("workflow")} />}
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
