"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, FolderInput, LockKeyhole, Pencil, Plus, Upload } from "lucide-react";
import { api, getApiErrorMessage, type IntegrationSource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { knowledgeApi, type KnowledgeSource, type SourceInput } from "@/lib/knowledge-api";
import { sourceGroups } from "@/lib/integrations/source-groups";
import { SourceCard } from "@/components/integrations/source-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { UploadDialog } from "./upload-dialog";
import { KnowledgeError } from "./shared";

const selectClass = "h-9 w-full rounded-lg border border-input bg-panel px-3 text-[13px] outline-none focus:ring-1 focus:ring-ring";

export function SourceManager({ sources, configured, connections = [], onBrowse, onWorkflow, documentsEnabled = true }: {
  sources: KnowledgeSource[]; configured: boolean; connections?: IntegrationSource[];
  onBrowse?: (group: string) => void; onWorkflow?: () => void; documentsEnabled?: boolean;
}) {
  const { user } = useAuth();
  const groups = useQuery({ queryKey: ["knowledge", "groups", user?.id], queryFn: knowledgeApi.groups, retry: false, enabled: documentsEnabled });
  const qc = useQueryClient();
  const [editing, setEditing] = useState<KnowledgeSource | "new" | null>(null);
  const [upload, setUpload] = useState(false);
  const [notice, setNotice] = useState("");
  const changed = () => { void qc.invalidateQueries({ queryKey: ["integrations"] }); void qc.invalidateQueries({ queryKey: ["knowledge"] }); void qc.invalidateQueries({ queryKey: ["workflow-status"] }); };
  const grouped = sourceGroups(connections, groups.data?.results ?? []);
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="max-w-2xl text-[13px] text-muted-foreground">Connect systems, choose content and manage access. Schedules and processing live in Workflow.</p>
      {documentsEnabled && <div className="flex gap-2"><Button variant="outline" size="sm" disabled={!configured} onClick={() => setUpload(true)}><Upload /> Upload documents</Button><Button variant="brand" size="sm" onClick={() => setEditing("new")}><Plus /> Add content source</Button></div>}
    </div>
    {notice && <p role="status" className="text-sm">{notice} <Button variant="link" onClick={onWorkflow}>View Workflow</Button></p>}
    {groups.isLoading && <LoadingState label="Loading sources…" />}
    {groups.isError && <KnowledgeError message="Document sources could not be loaded." retry={() => { void groups.refetch(); }} />}
    {!groups.isPending && !groups.isError && !grouped.length && <EmptyState title="Create your first source" hint="Add an upload collection or import a prepared snapshot." icon={<FolderInput className="h-8 w-8" />} />}
    {grouped.map((group) => <section key={group.id} className="overflow-hidden rounded-xl border border-line bg-card">
      {group.connection ? <SourceCard source={group.connection} onChanged={changed} onBrowseContent={() => onBrowse?.(group.id)} /> : <div className="flex flex-wrap items-center justify-between gap-3 p-5"><div><h3 className="text-sm font-semibold">{group.name}</h3><p className="mt-1 text-xs text-muted-foreground">{group.provider === "upload" ? "Files uploaded to DataGov" : "Imported snapshots · refresh reprocesses the prepared import, not the live provider"}</p></div><Button size="sm" variant="outline" onClick={() => onBrowse?.(group.id)}><Database /> Browse content</Button></div>}
      <details className="border-t border-line">
        <summary className="cursor-pointer px-5 py-3 text-xs font-medium">{group.collections.length} collections · {group.document_count.toLocaleString()} documents · Access & settings</summary>
        <div className="grid gap-3 border-t border-line p-4 lg:grid-cols-2">
          {sources.filter((source) => group.collections.some((collection) => collection.id === source.id)).map((source) => <article key={source.id} className="rounded-lg border border-line p-4">
            <div className="flex items-start justify-between gap-3"><h4 className="text-sm font-medium">{source.name}</h4><Badge variant={source.is_active ? "success" : "default"}>{source.is_active ? "Access enabled" : "Access disabled"}</Badge></div>
            <p className="my-3 flex items-center gap-2 text-xs text-muted-foreground"><LockKeyhole className="h-3.5 w-3.5" />{source.access === "restricted" ? "Selected members and groups" : "Organization members with assistant access"}</p>
            <Button variant="outline" size="sm" onClick={() => setEditing(source)}><Pencil /> Settings & access</Button>
          </article>)}
          {!group.collections.length && <p className="text-xs text-muted-foreground">No document collections. Catalog extraction is configured above.</p>}
        </div>
      </details>
    </section>)}
    <Dialog open={!!editing} onOpenChange={(open) => { if (!open) setEditing(null); }}><DialogContent><DialogHeader><DialogTitle>{editing === "new" ? "Add content source" : "Collection settings"}</DialogTitle><DialogDescription>Choose content, its source and who can access it.</DialogDescription></DialogHeader>{editing && <SourceForm key={editing === "new" ? "new" : editing.id} source={editing === "new" ? undefined : editing} done={() => setEditing(null)} />}</DialogContent></Dialog>
    <UploadDialog open={upload} onOpenChange={setUpload} sources={sources} onUploaded={(unchanged) => setNotice(unchanged ? "This document already exists; no duplicate was created." : "Upload received. Processing is queued in Workflow.")} />
  </div>;
}

export function SourceForm({ source, done }: { source?: KnowledgeSource; done: () => void }) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [name, setName] = useState(source?.name || "");
  const [kind, setKind] = useState<KnowledgeSource["kind"]>(source?.kind || "upload");
  const [path, setPath] = useState(String(source?.config?.path || ""));
  const [integrationId, setIntegrationId] = useState(source?.integration_id ? String(source.integration_id) : "");
  const [importGroup, setImportGroup] = useState(source?.import_group || "");
  const connections = useQuery({ queryKey: ["knowledge", "connections", user?.id], queryFn: knowledgeApi.connections });
  const [active, setActive] = useState(source?.is_active ?? true);
  const [access, setAccess] = useState<KnowledgeSource["access"]>(source?.access || "restricted");
  const [userIds, setUserIds] = useState(source?.allowed_user_ids || (user ? [user.id] : []));
  const [groupIds, setGroupIds] = useState(source?.allowed_group_ids || []);
  const members = useQuery({ queryKey: ["knowledge", "access-options", user?.id], queryFn: api.org.members, enabled: access === "restricted", retry: false });
  const save = useMutation({
    mutationFn: (body: Partial<SourceInput>) => source ? knowledgeApi.updateSource(source.id, body) : knowledgeApi.createSource({ cron: "", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC", ...body } as SourceInput),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["knowledge"] }); done(); },
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    save.mutate({ name: name.trim(), kind, config: kind === "snapshot" ? { ...source?.config, path: path.trim() } : source?.config || {},
      is_active: active, access,
      integration_id: integrationId ? Number(integrationId) : null, import_group: importGroup.trim(),
      allowed_user_ids: access === "restricted" ? userIds : [], allowed_group_ids: access === "restricted" ? groupIds : [] });
  }
  function toggleId(list: number[], id: number) { return list.includes(id) ? list.filter((item) => item !== id) : [...list, id]; }

  return <form onSubmit={submit} className="space-y-4">
    <label className="block space-y-1.5 text-[13px] font-medium">Source name<Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Company handbook" maxLength={200} required /></label>
    <label className="block space-y-1.5 text-[13px] font-medium">Source type
      <select value={kind} onChange={(e) => setKind(e.target.value as KnowledgeSource["kind"])} disabled={!!source} className={selectClass}>
        <option value="upload">Uploaded documents / integration output</option><option value="snapshot">Snapshot import</option>
      </select>
    </label>
    <label className="block space-y-1.5 text-[13px] font-medium">Connection
      <select className={selectClass} value={integrationId} onChange={(e) => { setIntegrationId(e.target.value); }}>
        <option value="">Independent collection</option>
        {connections.data?.results.map((connection) => <option key={connection.id} value={connection.id}>{connection.name}</option>)}
      </select>
    </label>
    {!integrationId && <label className="block space-y-1.5 text-[13px] font-medium">Import group<Input value={importGroup} onChange={(e) => setImportGroup(e.target.value)} placeholder="e.g. Confluence — company knowledge" maxLength={200} /></label>}
    {kind === "snapshot" && <label className="block space-y-1.5 text-[13px] font-medium">Snapshot manifest path
      <Input required value={path} onChange={(e) => setPath(e.target.value)} placeholder="company/manifest.json" />
      <span className="block text-xs font-normal text-muted-foreground">A prepared manifest inside your organization’s import folder, managed by your administrator. Enter its path relative to that folder.</span>
    </label>}
    <p className="text-xs text-muted-foreground">Scheduling and indexing are managed in Workflow.</p>
    <label className="flex items-center gap-2 text-[13px]"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} className="accent-brand" /> Content access enabled</label>
    <label className="block space-y-1.5 text-[13px] font-medium">Document access
      <select value={access} onChange={(e) => setAccess(e.target.value as KnowledgeSource["access"])} className={selectClass}>
        <option value="restricted">Selected members and groups</option><option value="organization">Organization</option>
      </select>
    </label>
    <p className="text-xs text-muted-foreground">Access applies to search, answers, original files, and shared document links. Organization administrators can manage sources.</p>
    {access === "restricted" && <div className="rounded-lg border border-line p-3">
      {members.isPending && <p className="text-xs text-muted-foreground">Loading members…</p>}
      {members.isError && <KnowledgeError message="Member choices could not be loaded. Existing access selections will be preserved." retry={() => { void members.refetch(); }} />}
      {members.data && <div className="grid max-h-52 gap-4 overflow-y-auto sm:grid-cols-2">
        <fieldset className="space-y-2"><legend className="mb-2 text-xs font-semibold">Members</legend>
          {members.data.members.map((member) => <label key={member.user_id} className="flex items-start gap-2 text-xs"><input className="mt-0.5 accent-brand" type="checkbox" checked={userIds.includes(member.user_id)} onChange={() => setUserIds(toggleId(userIds, member.user_id))} /><span>{member.display_name || member.username}<span className="block text-faint">{member.email}</span></span></label>)}
        </fieldset>
        <fieldset className="space-y-2"><legend className="mb-2 text-xs font-semibold">Groups</legend>
          {members.data.available_groups.map((group) => <label key={group.id} className="flex items-start gap-2 text-xs"><input className="accent-brand" type="checkbox" checked={groupIds.includes(group.id)} onChange={() => setGroupIds(toggleId(groupIds, group.id))} />{group.name}</label>)}
          {!members.data.available_groups.length && <p className="text-xs text-muted-foreground">No groups available.</p>}
        </fieldset>
      </div>}
      {!userIds.length && !groupIds.length && <p className="mt-3 text-xs text-warn">Select at least one member or group that can read these documents.</p>}
    </div>}
    {save.isError && <KnowledgeError message={getApiErrorMessage(save.error, "The source could not be saved.")} />}
    <div className="flex justify-end gap-2 border-t border-line pt-4"><Button variant="outline" type="button" onClick={done}>Cancel</Button><Button variant="brand" type="submit" disabled={save.isPending || !name.trim() || access === "restricted" && !userIds.length && !groupIds.length}>{save.isPending ? "Saving…" : "Save source"}</Button></div>
  </form>;
}
