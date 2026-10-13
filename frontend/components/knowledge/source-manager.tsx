"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, FolderInput, LockKeyhole, Pencil, Play, Plus, Upload } from "lucide-react";
import { api, getApiErrorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { knowledgeApi, type KnowledgeSource, type SourceInput } from "@/lib/knowledge-api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { KnowledgeError, displayDate } from "./shared";

const selectClass = "h-9 w-full rounded-lg border border-input bg-panel px-3 text-[13px] outline-none focus:ring-1 focus:ring-ring";

export function SourceManager({ sources, configured }: { sources: KnowledgeSource[]; configured: boolean }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<KnowledgeSource | "new" | null>(null);
  const [notice, setNotice] = useState("");
  const sync = useMutation({
    mutationFn: knowledgeApi.syncSource,
    onSuccess: () => {
      setNotice("Refresh queued. Follow its progress in Processing runs.");
      void qc.invalidateQueries({ queryKey: ["knowledge"] });
    },
  });
  const toggle = useMutation({
    mutationFn: (source: KnowledgeSource) => knowledgeApi.updateSource(source.id, { is_active: !source.is_active }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["knowledge", "sources"] }); },
  });

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="max-w-2xl text-[13px] text-muted-foreground">Organize uploads and import document snapshots. DataGov manages refresh schedules and access for each source.</p>
      <Button variant="brand" size="sm" onClick={() => setEditing("new")}><Plus /> Add source</Button>
    </div>
    {notice && <p role="status" className="rounded-lg border border-brand/20 bg-brand/5 px-4 py-3 text-[13px]">{notice}</p>}
    {(sync.isError || toggle.isError) && <KnowledgeError message={getApiErrorMessage(sync.error || toggle.error, "The source could not be updated.")} />}
    {!sources.length && <EmptyState title="Create your first source" hint="Start with a PDF upload collection, or import a prepared document snapshot." icon={<FolderInput className="h-8 w-8" />} />}
    <div className="grid gap-4 xl:grid-cols-2">
      {sources.map((source) => <article key={source.id} className="rounded-xl border border-line bg-card p-5 shadow-card">
        <div className="flex items-start gap-3">
          <div className="rounded-lg bg-brand/10 p-2.5 text-brand">{source.kind === "upload" ? <Upload className="h-5 w-5" /> : <FolderInput className="h-5 w-5" />}</div>
          <div className="min-w-0 flex-1"><h3 className="truncate text-[14px] font-semibold">{source.name}</h3><p className="mt-1 text-xs text-muted-foreground">{source.kind === "upload" ? "PDF upload collection" : "Snapshot import"}</p></div>
          <Badge variant={source.is_active ? "success" : "default"} dot>{source.is_active ? "Active" : "Paused"}</Badge>
        </div>
        <dl className="my-4 space-y-2 text-xs text-muted-foreground">
          <div className="flex items-start gap-2"><CalendarClock className="h-3.5 w-3.5 shrink-0" /><dt className="sr-only">Schedule</dt><dd>{source.cron ? <><span className="font-mono text-foreground">{source.cron}</span> · {source.timezone}</> : "Manual refresh"}</dd></div>
          <div className="flex items-center gap-2"><LockKeyhole className="h-3.5 w-3.5" /><dt className="sr-only">Access</dt><dd>{source.access === "restricted" ? "Restricted to selected members and groups" : "Organization members with AI Assistant access"}</dd></div>
          <div><dt className="inline">Last run: </dt><dd className="inline">{displayDate(source.last_run_at)}</dd></div>
        </dl>
        <div className="flex flex-wrap gap-2 border-t border-line pt-3">
          <Button variant="outline" size="sm" onClick={() => setEditing(source)}><Pencil /> Settings</Button>
          <Button variant="outline" size="sm" disabled={toggle.isPending} onClick={() => toggle.mutate(source)}>{source.is_active ? "Pause" : "Resume"}</Button>
          <Button variant="outline" size="sm" disabled={!configured || !source.is_active || sync.isPending} onClick={() => { setNotice(""); sync.mutate(source.id); }}><Play /> Refresh now</Button>
        </div>
      </article>)}
    </div>
    <Dialog open={!!editing} onOpenChange={(open) => { if (!open) setEditing(null); }}>
      <DialogContent><DialogHeader><DialogTitle>{editing === "new" ? "Add document source" : "Source settings"}</DialogTitle>
        <DialogDescription>Choose who can find these documents and when they refresh.</DialogDescription></DialogHeader>
        {editing && <SourceForm key={editing === "new" ? "new" : editing.id} source={editing === "new" ? undefined : editing} done={() => setEditing(null)} />}
      </DialogContent>
    </Dialog>
  </div>;
}

function SourceForm({ source, done }: { source?: KnowledgeSource; done: () => void }) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [name, setName] = useState(source?.name || "");
  const [kind, setKind] = useState<KnowledgeSource["kind"]>(source?.kind || "upload");
  const [path, setPath] = useState(String(source?.config?.path || ""));
  const [cron, setCron] = useState(source?.cron || "");
  const [timezone, setTimezone] = useState(source?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [active, setActive] = useState(source?.is_active ?? true);
  const [access, setAccess] = useState<KnowledgeSource["access"]>(source?.access || "restricted");
  const [userIds, setUserIds] = useState(source?.allowed_user_ids || (user ? [user.id] : []));
  const [groupIds, setGroupIds] = useState(source?.allowed_group_ids || []);
  const members = useQuery({ queryKey: ["knowledge", "access-options", user?.id], queryFn: api.org.members, enabled: access === "restricted", retry: false });
  const save = useMutation({
    mutationFn: (body: SourceInput) => source ? knowledgeApi.updateSource(source.id, body) : knowledgeApi.createSource(body),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["knowledge"] }); done(); },
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    save.mutate({ name: name.trim(), kind, config: kind === "snapshot" ? { ...source?.config, path: path.trim() } : source?.config || {},
      cron: cron.trim(), timezone: timezone.trim(), is_active: active, access,
      allowed_user_ids: access === "restricted" ? userIds : [], allowed_group_ids: access === "restricted" ? groupIds : [] });
  }
  function toggleId(list: number[], id: number) { return list.includes(id) ? list.filter((item) => item !== id) : [...list, id]; }

  return <form onSubmit={submit} className="space-y-4">
    <label className="block space-y-1.5 text-[13px] font-medium">Source name<Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Company handbook" maxLength={200} required /></label>
    <label className="block space-y-1.5 text-[13px] font-medium">Source type
      <select value={kind} onChange={(e) => setKind(e.target.value as KnowledgeSource["kind"])} disabled={!!source} className={selectClass}>
        <option value="upload">PDF upload collection</option><option value="snapshot">Snapshot import</option>
      </select>
    </label>
    {kind === "snapshot" && <label className="block space-y-1.5 text-[13px] font-medium">Snapshot manifest path
      <Input required value={path} onChange={(e) => setPath(e.target.value)} placeholder="company/manifest.json" />
      <span className="block text-xs font-normal text-muted-foreground">A prepared manifest inside your organization’s import folder, managed by your administrator. Enter its path relative to that folder.</span>
    </label>}
    <div className="rounded-lg border border-line bg-panel/50 p-3">
      <h3 className="mb-3 text-[13px] font-semibold">Refresh schedule</h3>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block space-y-1.5 text-xs">Cron expression<Input value={cron} onChange={(e) => setCron(e.target.value)} placeholder="Manual only" /></label>
        <label className="block space-y-1.5 text-xs">Time zone<Input value={timezone} onChange={(e) => setTimezone(e.target.value)} placeholder="Europe/Athens" required /></label>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">Leave blank for manual refresh. For daily at 06:00, use <code>0 6 * * *</code>. Missed refreshes are combined into one run.</p>
      <label className="mt-3 flex items-center gap-2 text-[13px]"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} className="accent-brand" /> Source active</label>
    </div>
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
