"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request, getApiErrorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { LoadingState } from "@/components/ui/misc";

// "analyzer" is the documents tool: the Document Analyzer answers every document question.
export const ASSISTANT_TOOLS: Record<string, string> = {
  analyzer: "Documents", powerbi: "Power BI catalog", dbt: "dbt catalog",
  bigquery: "BigQuery schema", lineage: "Catalog lineage",
  powerbi_live: "Live Power BI queries", bigquery_live: "Live BigQuery queries",
};

type Tool = { id: string; label: string; description: string; allowed: boolean; selected: boolean; available: boolean; effective: boolean };

export function AssistantTools() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const key = ["assistant-tools", user?.id];
  const query = useQuery({ queryKey: key, queryFn: () => request<{ tools: Tool[] }>("/me/assistant-tools/"), retry: false });
  const save = useMutation({
    mutationFn: ({ id, checked }: { id: string; checked: boolean }) => request<{ tools: Tool[] }>("/me/assistant-tools/", { method: "PATCH", body: JSON.stringify({ preferences: { [id]: checked } }) }),
    onSuccess: (data) => { qc.setQueryData(key, data); void qc.invalidateQueries({ queryKey: ["knowledge", "status"] }); },
  });
  return <Card><CardHeader><CardTitle>AI assistant tools</CardTitle></CardHeader><CardContent className="space-y-3">
    <p className="text-xs text-muted-foreground">Choose which tools the assistant may use for you. Your administrator controls access; document permissions still apply.</p>
    {query.isPending && <LoadingState label="Loading tools…" />}
    {query.isError && <p role="alert" className="text-sm text-err">Tools could not be loaded.</p>}
    {query.data?.tools.map((tool) => <label key={tool.id} className="flex items-start justify-between gap-4 border-b border-line py-2 last:border-0"><span><span className="text-sm font-medium">{tool.label}</span><span className="mt-1 block text-xs text-muted-foreground">{tool.id === "analyzer" && !tool.available ? tool.description : !tool.allowed ? "Disabled by your administrator" : !tool.available ? "Not enabled for this organization" : tool.selected && !tool.effective ? "Enable its catalog tool first" : tool.description}</span></span><Switch aria-label={tool.label} checked={tool.allowed && tool.available && tool.selected} disabled={save.isPending || !tool.allowed || !tool.available} onCheckedChange={(checked) => save.mutate({ id: tool.id, checked })} /></label>)}
    {save.isError && <p role="alert" className="text-sm text-err">{getApiErrorMessage(save.error, "Tool preferences could not be saved.")}</p>}
  </CardContent></Card>;
}

export function AssistantToolAccess({ value, onChange }: { value: Record<string, boolean>; onChange: (value: Record<string, boolean>) => void }) {
  return <details className="rounded-lg border border-line p-3"><summary className="cursor-pointer text-sm font-medium">AI tool access</summary><p className="my-3 text-xs text-muted-foreground">Organization settings remain the upper limit. The member can switch permitted tools off in User Settings.</p>
    <div className="grid gap-3 sm:grid-cols-2">{Object.entries(ASSISTANT_TOOLS).map(([id, label]) => <label key={id} className="flex items-center justify-between gap-3 text-xs">{label}<Switch aria-label={`Allow ${label}`} checked={value[id] !== false} onCheckedChange={(checked) => onChange({ ...value, [id]: checked })} /></label>)}</div>
  </details>;
}
