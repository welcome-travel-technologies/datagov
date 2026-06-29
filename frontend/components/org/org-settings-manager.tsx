"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserPlus, Pencil, Trash2, ShieldCheck, Save, AlertCircle } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { MemberDialog } from "@/components/org/member-dialog";
import {
  api,
  getApiErrorMessage,
  type ChatbotModelRef,
  type OrgMember,
  type OrgSettings,
  type ScopeOption,
} from "@/lib/api";

const QK = ["org-members"];

export function OrgSettingsManager() {
  const qc = useQueryClient();
  const { data, isLoading, isError } = useQuery({ queryKey: QK, queryFn: api.org.members });

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<OrgMember | null>(null);

  const removeMut = useMutation({
    mutationFn: (userId: number) => api.org.removeMember(userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK }),
  });

  const groupName = useMemo(() => {
    const m = new Map<number, string>();
    (data?.available_groups ?? []).forEach((g) => m.set(g.id, g.name));
    return m;
  }, [data?.available_groups]);

  if (isLoading) return <LoadingState label="Loading organization…" />;
  if (isError || !data) {
    return (
      <Card>
        <CardContent className="pt-6">
          <EmptyState
            title="Couldn't load organization settings"
            hint="This area requires Organization Settings (Admin) access."
          />
        </CardContent>
      </Card>
    );
  }

  function openAdd() {
    setEditing(null);
    setDialogOpen(true);
  }
  function openEdit(m: OrgMember) {
    setEditing(m);
    setDialogOpen(true);
  }
  function remove(m: OrgMember) {
    if (window.confirm(`Remove ${m.display_name || m.email} from ${data!.organization.name}?`)) {
      removeMut.mutate(m.user_id);
    }
  }

  return (
    <>
      <Tabs defaultValue="members">
        <TabsList>
          <TabsTrigger value="members">Members ({data.members.length})</TabsTrigger>
          <TabsTrigger value="settings">Assistant &amp; Display</TabsTrigger>
        </TabsList>

        <TabsContent value="members">
          <div className="mb-3 flex items-center justify-between">
            <p className="text-[13px] text-muted-foreground">
              Manage logins, page access, and governance roles for{" "}
              <span className="font-medium text-foreground">{data.organization.name}</span>.
            </p>
            <Button variant="brand" onClick={openAdd}>
              <UserPlus /> Add member
            </Button>
          </div>

          {removeMut.isError && (
            <div className="mb-3 flex items-center gap-2 rounded-md bg-err/10 px-3 py-2 text-[12.5px] text-err">
              <AlertCircle className="h-3.5 w-3.5" />
              {getApiErrorMessage(removeMut.error, "Could not remove member.")}
            </div>
          )}

          <Card className="overflow-hidden">
            {data.members.length === 0 ? (
              <EmptyState title="No members yet" hint="Add your first organization member." />
            ) : (
              <Table>
                <THead>
                  <TR>
                    <TH>Member</TH>
                    <TH>Page access</TH>
                    <TH>Roles</TH>
                    <TH className="text-right">Actions</TH>
                  </TR>
                </THead>
                <TBody>
                  {data.members.map((m) => (
                    <TR key={m.user_id}>
                      <TD>
                        <div className="flex items-center gap-2">
                          <div>
                            <div className="flex items-center gap-1.5 font-medium">
                              {m.display_name || m.email}
                              {m.is_admin && (
                                <ShieldCheck className="h-3.5 w-3.5 text-brand" aria-label="Org admin" />
                              )}
                              {m.is_self && <span className="text-[11px] text-faint">(you)</span>}
                            </div>
                            <div className="text-[12px] text-muted-foreground">{m.email}</div>
                          </div>
                        </div>
                      </TD>
                      <TD>
                        <div className="flex flex-wrap gap-1">
                          {m.group_ids.length === 0 && (
                            <span className="text-[12px] text-faint">No page access</span>
                          )}
                          {m.group_ids.map((gid) => (
                            <Badge key={gid} variant="outline">
                              {groupName.get(gid) ?? `#${gid}`}
                            </Badge>
                          ))}
                        </div>
                      </TD>
                      <TD>
                        <div className="flex flex-wrap gap-1">
                          {m.is_owner && <Badge variant="brand">Owner</Badge>}
                          {m.is_steward && <Badge variant="info">Steward</Badge>}
                          {m.is_other && <Badge>Other</Badge>}
                          {!m.is_owner && !m.is_steward && !m.is_other && (
                            <span className="text-[12px] text-faint">—</span>
                          )}
                        </div>
                      </TD>
                      <TD className="text-right">
                        <div className="flex justify-end gap-1">
                          <Button size="sm" variant="ghost" onClick={() => openEdit(m)}>
                            <Pencil /> Edit
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-err hover:bg-err/10 hover:text-err"
                            disabled={m.is_self || removeMut.isPending}
                            title={m.is_self ? "You can't remove yourself" : "Remove member"}
                            onClick={() => remove(m)}
                          >
                            <Trash2 /> Remove
                          </Button>
                        </div>
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )}
          </Card>
        </TabsContent>

        <TabsContent value="settings">
          <SettingsPanel
            settings={data.settings}
            models={data.chatbot_models}
            onSaved={() => qc.invalidateQueries({ queryKey: QK })}
          />
        </TabsContent>
      </Tabs>

      <MemberDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        member={editing}
        groups={data.available_groups}
        departments={data.departments}
        onSaved={() => qc.invalidateQueries({ queryKey: QK })}
      />
    </>
  );
}

const NONE = "none";

function SettingsPanel({
  settings,
  models,
  onSaved,
}: {
  settings: OrgSettings;
  models: ChatbotModelRef[];
  onSaved: () => void;
}) {
  const [form, setForm] = useState<OrgSettings>(settings);
  useEffect(() => setForm(settings), [settings]);

  const saveMut = useMutation({
    mutationFn: () => api.org.saveSettings(form),
    onSuccess: onSaved,
  });

  const set = (patch: Partial<OrgSettings>) => setForm((f) => ({ ...f, ...patch }));

  // Available workspaces / datasets for the context-scope selectors. Only
  // fetched once a scoped integration is enabled.
  const scopeQ = useQuery({
    queryKey: ["org-assistant-scope"],
    queryFn: api.org.assistantScope,
    enabled: form.powerbi_tools_enabled || form.bigquery_tools_enabled,
  });

  const toggleScope = (
    key: "assistant_powerbi_workspace_ids" | "assistant_bigquery_dataset_ids",
    id: string,
  ) =>
    setForm((f) => {
      const cur = f[key] ?? [];
      return {
        ...f,
        [key]: cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id],
      };
    });

  return (
    <div className="max-w-2xl space-y-4">
      <Card>
        <CardContent className="space-y-1 pt-5">
          <h3 className="mb-2 text-[13px] font-semibold">AI Assistant tools</h3>
          <ToggleRow
            label="PowerBI tools"
            hint="Profile measures/reports and build measure↔report usage analytics from the catalog. No external calls."
            checked={form.powerbi_tools_enabled}
            onChange={(v) => set({ powerbi_tools_enabled: v })}
          />
          <ToggleRow
            label="PowerBI live tools"
            hint="Let the assistant run live PowerBI REST queries (DAX)."
            checked={form.powerbi_live_tools_enabled}
            onChange={(v) => set({ powerbi_live_tools_enabled: v })}
          />
          <ToggleRow
            label="dbt tools"
            hint="Models, columns, SQL, and lineage from the local catalog. No external calls."
            checked={form.dbt_tools_enabled}
            onChange={(v) => set({ dbt_tools_enabled: v })}
          />
          <ToggleRow
            label="BigQuery tools"
            hint="Load dataset schema into the assistant (read-only). No query execution."
            checked={form.bigquery_tools_enabled}
            onChange={(v) => set({ bigquery_tools_enabled: v })}
          />
          <ToggleRow
            label="BigQuery live tools"
            hint="Allow read-only live BigQuery SQL queries."
            checked={form.bigquery_live_tools_enabled}
            onChange={(v) => set({ bigquery_live_tools_enabled: v })}
          />
          <ToggleRow
            label="Debug responses"
            hint="Append DAX / SQL / tool-call diagnostics to every answer."
            checked={form.debug_responses_enabled}
            onChange={(v) => set({ debug_responses_enabled: v })}
          />

          <div className="flex items-center justify-between gap-4 border-t border-line pt-3">
            <div>
              <div className="text-[13px] font-medium">Assistant model</div>
              <div className="text-[12px] text-muted-foreground">Which LLM powers the AI Assistant.</div>
            </div>
            <div className="w-56">
              <Select
                value={form.chatbot_model_id ? String(form.chatbot_model_id) : NONE}
                onValueChange={(v) =>
                  set({ chatbot_model_id: v === NONE ? null : Number(v) })
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="Default" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>Organization default</SelectItem>
                  {models.map((m) => (
                    <SelectItem key={m.id} value={String(m.id)}>
                      {m.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex items-center justify-between gap-4 border-t border-line pt-3">
            <div>
              <div className="text-[13px] font-medium">Response timeout</div>
              <div className="text-[12px] text-muted-foreground">
                Max seconds per question before the assistant times out (30–600).
              </div>
            </div>
            <input
              type="number"
              min={30}
              max={600}
              value={form.chat_timeout_seconds ?? 180}
              onChange={(e) =>
                set({ chat_timeout_seconds: Number(e.target.value) || 180 })
              }
              onBlur={(e) =>
                set({
                  chat_timeout_seconds: Math.max(
                    30,
                    Math.min(600, Number(e.target.value) || 180),
                  ),
                })
              }
              className="w-24 rounded-md border border-line bg-transparent px-2 py-1 text-right text-[13px]"
            />
          </div>
        </CardContent>
      </Card>

      {(form.powerbi_tools_enabled || form.bigquery_tools_enabled) && (
        <Card>
          <CardContent className="space-y-4 pt-5">
            <div>
              <h3 className="text-[13px] font-semibold">Assistant context scope</h3>
              <p className="text-[12px] text-muted-foreground">
                Which workspaces / datasets are loaded into the assistant&apos;s
                context. Keeping this tight reduces tokens and keeps answers focused.
              </p>
            </div>
            {scopeQ.isLoading && (
              <div className="text-[12px] text-muted-foreground">Loading options…</div>
            )}
            {form.powerbi_tools_enabled && (
              <CheckboxList
                title="PowerBI workspaces"
                hint="Leave all unchecked to include every workspace."
                options={scopeQ.data?.powerbi ?? []}
                selected={form.assistant_powerbi_workspace_ids ?? []}
                onToggle={(id) => toggleScope("assistant_powerbi_workspace_ids", id)}
              />
            )}
            {form.bigquery_tools_enabled && (
              <CheckboxList
                title="BigQuery datasets"
                hint="Select the datasets whose schema the assistant should see."
                options={scopeQ.data?.bigquery ?? []}
                selected={form.assistant_bigquery_dataset_ids ?? []}
                onToggle={(id) => toggleScope("assistant_bigquery_dataset_ids", id)}
              />
            )}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="space-y-1 pt-5">
          <h3 className="mb-2 text-[13px] font-semibold">Display</h3>
          <ToggleRow
            label="Show deleted items"
            hint="Keep counting/listing items removed at source instead of hiding them."
            checked={form.show_deleted_items}
            onChange={(v) => set({ show_deleted_items: v })}
          />
        </CardContent>
      </Card>

      <div className="flex items-center gap-3">
        <Button variant="brand" onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
          <Save /> {saveMut.isPending ? "Saving…" : "Save settings"}
        </Button>
        {saveMut.isSuccess && <span className="text-[12.5px] text-ok">Saved.</span>}
        {saveMut.isError && <span className="text-[12.5px] text-err">Save failed.</span>}
      </div>
    </div>
  );
}

function ToggleRow({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div>
        <div className="text-[13px] font-medium">{label}</div>
        <div className="text-[12px] text-muted-foreground">{hint}</div>
      </div>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function CheckboxList({
  title,
  hint,
  options,
  selected,
  onToggle,
}: {
  title: string;
  hint: string;
  options: ScopeOption[];
  selected: string[];
  onToggle: (id: string) => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-[13px] font-medium">{title}</div>
      <div className="text-[12px] text-muted-foreground">{hint}</div>
      {options.length === 0 ? (
        <div className="text-[12px] text-muted-foreground">No options available.</div>
      ) : (
        <div className="max-h-48 space-y-0.5 overflow-y-auto rounded-lg border border-line bg-panel2/40 p-2">
          {options.map((o) => (
            <label
              key={o.id}
              className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-[13px] hover:bg-foreground/[0.04]"
            >
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-brand"
                checked={selected.includes(o.id)}
                onChange={() => onToggle(o.id)}
              />
              <span className="truncate">{o.name}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
