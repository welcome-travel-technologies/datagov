"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownToLine, Layers, Plus, Search, Trash2, X } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  getApiErrorMessage,
  statusLabel,
  unwrapResults,
  type DataPerson,
  type Definition,
  type DefinitionApplyResult,
  type Department,
  type Item,
} from "@/lib/api";

export default function DefinitionsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [assignOpen, setAssignOpen] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const defsQ = useQuery({
    queryKey: ["definitions"],
    queryFn: () => api.definitions.list({ limit: 1000 }),
  });
  const metaQ = useQuery({
    queryKey: ["definition-meta"],
    queryFn: async () => {
      const [people, depts] = await Promise.all([
        api.dataPersons.list({ is_owner: true }),
        api.departments.list(),
      ]);
      return {
        people: unwrapResults<DataPerson>(people),
        departments: unwrapResults<Department>(depts),
      };
    },
    staleTime: 5 * 60_000,
  });

  const definitions = unwrapResults<Definition>(defsQ.data);
  const people = metaQ.data?.people ?? [];
  const departments = metaQ.data?.departments ?? [];
  const selected = definitions.find((d) => d.id === selectedId) ?? null;

  function refresh() {
    qc.invalidateQueries({ queryKey: ["definitions"] });
    qc.invalidateQueries({ queryKey: ["definition-groups"] });
  }

  const saveMut = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<Definition> }) =>
      api.definitions.update(id, body),
    onSuccess: refresh,
    onError: (e) => setNote(getApiErrorMessage(e, "Could not save.")),
  });
  const deleteMut = useMutation({
    mutationFn: (id: number) => api.definitions.remove(id),
    onSuccess: () => {
      setSelectedId(null);
      setNote("Definition deleted. Its measures kept their metadata.");
      refresh();
    },
  });

  return (
    <div>
      <PageHeader
        title="Definitions"
        description="Group measures under a shared business definition. A definition does nothing on its own — assigning measures changes no governance. Metadata moves down only when you run an action."
        actions={
          <Button variant="brand" size="sm" onClick={() => setCreating(true)}>
            <Plus /> New definition
          </Button>
        }
      />

      {note && (
        <div className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-line bg-panel2/50 px-4 py-2 text-[12px]">
          <span>{note}</span>
          <button className="text-faint hover:text-foreground" onClick={() => setNote(null)}>
            Dismiss
          </button>
        </div>
      )}

      {defsQ.isLoading && (
        <Card>
          <LoadingState label="Loading definitions…" />
        </Card>
      )}
      {!defsQ.isLoading && definitions.length === 0 && (
        <Card>
          <EmptyState
            title="No definitions yet"
            hint="Create one, then assign the measure groups that express it."
          />
        </Card>
      )}

      {definitions.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          {/* List */}
          <Card className="overflow-hidden">
            <div className="border-b border-line bg-panel2/50 px-4 py-3">
              <h3 className="text-[13px] font-bold uppercase tracking-wide">
                Definitions <Badge variant="brand">{definitions.length}</Badge>
              </h3>
            </div>
            <div className="max-h-[70vh] overflow-y-auto">
              {definitions.map((d) => (
                <button
                  key={d.id}
                  onClick={() => setSelectedId(d.id)}
                  className={`flex w-full items-center justify-between gap-2 border-b border-line px-4 py-2.5 text-left text-[13px] hover:bg-panel2 ${
                    d.id === selectedId ? "bg-panel2" : ""
                  }`}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{d.name}</span>
                    <span className="block truncate text-[11px] text-faint">
                      {d.ownership_person_name || "no owner"}
                      {d.ownership_department_name ? ` · ${d.ownership_department_name}` : ""}
                    </span>
                  </span>
                  <Badge>{d.group_count}</Badge>
                </button>
              ))}
            </div>
          </Card>

          {/* Detail */}
          {selected ? (
            <DefinitionDetail
              key={selected.id}
              definition={selected}
              people={people}
              departments={departments}
              onSave={(body) => saveMut.mutate({ id: selected.id, body })}
              onDelete={() => deleteMut.mutate(selected.id)}
              onAssign={() => setAssignOpen(true)}
              onApplied={(msg) => {
                setNote(msg);
                refresh();
              }}
            />
          ) : (
            <Card>
              <EmptyState title="Pick a definition" hint="Select one on the left to see its measures." />
            </Card>
          )}
        </div>
      )}

      <CreateDialog
        open={creating}
        onOpenChange={setCreating}
        onCreated={(d) => {
          setSelectedId(d.id);
          refresh();
        }}
      />
      {selected && (
        <AssignDialog
          open={assignOpen}
          onOpenChange={setAssignOpen}
          definition={selected}
          onDone={refresh}
        />
      )}
    </div>
  );
}

function DefinitionDetail({
  definition,
  people,
  departments,
  onSave,
  onDelete,
  onAssign,
  onApplied,
}: {
  definition: Definition;
  people: DataPerson[];
  departments: Department[];
  onSave: (body: Partial<Definition>) => void;
  onDelete: () => void;
  onAssign: () => void;
  onApplied: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const [preview, setPreview] = useState<DefinitionApplyResult | null>(null);

  const groupsQ = useQuery({
    queryKey: ["definition-groups", definition.id],
    queryFn: () => api.definitions.groups(definition.id),
  });
  const groups = groupsQ.data ?? [];

  const applyMut = useMutation({
    mutationFn: (dry: boolean) => api.definitions.apply(definition.id, { dry_run: dry }),
    onSuccess: (res) => {
      if (res.dry_run) {
        setPreview(res);
        return;
      }
      setPreview(null);
      qc.invalidateQueries({ queryKey: ["definition-groups", definition.id] });
      onApplied(
        res.updated === 0
          ? "Nothing to change — every measure already matches."
          : `Owner and department applied to ${res.updated} measure group${res.updated === 1 ? "" : "s"}.`,
      );
    },
  });
  const unassignMut = useMutation({
    mutationFn: (groupId: number) => api.definitions.assign(definition.id, { remove: [groupId] }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["definition-groups", definition.id] });
      qc.invalidateQueries({ queryKey: ["definitions"] });
    },
  });

  const canApply = !!(definition.ownership_person || definition.ownership_department);

  return (
    <div className="space-y-4">
      <Card>
        <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2">
          <Field label="Name">
            <input
              defaultValue={definition.name}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v && v !== definition.name) onSave({ name: v });
              }}
              className="h-9 w-full rounded-md border border-input bg-panel px-3 text-[13px] outline-none"
            />
          </Field>
          <Field label="Description">
            <input
              defaultValue={definition.description ?? ""}
              onBlur={(e) => {
                if (e.target.value !== (definition.description ?? "")) {
                  onSave({ description: e.target.value });
                }
              }}
              placeholder="What this measures, in business terms…"
              className="h-9 w-full rounded-md border border-input bg-panel px-3 text-[13px] outline-none placeholder:text-faint"
            />
          </Field>
          <Field label="Owner">
            <SimpleSelect
              value={definition.ownership_person ? String(definition.ownership_person) : ""}
              onValueChange={(v) => onSave({ ownership_person: v ? Number(v) : null })}
              options={[
                { value: "", label: "— none —" },
                ...people.map((p) => ({ value: String(p.id), label: p.name })),
              ]}
            />
          </Field>
          <Field label="Department">
            <SimpleSelect
              value={definition.ownership_department ? String(definition.ownership_department) : ""}
              onValueChange={(v) => onSave({ ownership_department: v ? Number(v) : null })}
              options={[
                { value: "", label: "— none —" },
                ...departments.map((d) => ({ value: String(d.id), label: d.name })),
              ]}
            />
          </Field>
        </div>
      </Card>

      {/* Actions */}
      <Card>
        <div className="flex flex-wrap items-center gap-3 p-4">
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold">Inherit owner &amp; department</div>
            <div className="mt-0.5 text-[11px] text-faint">
              Writes this definition&apos;s owner and department onto all {definition.group_count}{" "}
              assigned measure group{definition.group_count === 1 ? "" : "s"}, replacing what they
              have. Fields left empty here are skipped, not cleared.
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            disabled={!canApply || applyMut.isPending}
            onClick={() => applyMut.mutate(true)}
          >
            Preview
          </Button>
          <Button
            variant="brand"
            size="sm"
            disabled={!canApply || applyMut.isPending}
            onClick={() => applyMut.mutate(false)}
          >
            <ArrowDownToLine /> Apply
          </Button>
        </div>
        {!canApply && (
          <div className="border-t border-line px-4 py-2 text-[11px] text-warn">
            Set an owner or a department first — there is nothing to push down yet.
          </div>
        )}
        {preview && (
          <div className="border-t border-line bg-panel2/50 px-4 py-2.5 text-[12px]">
            Would change <b>{preview.would_update}</b> of {preview.group_count} measure group
            {preview.group_count === 1 ? "" : "s"}.
            {preview.skipped_unset.length > 0 && (
              <span className="text-faint">
                {" "}
                Skipping {preview.skipped_unset.map((f) => f.replace("ownership_", "")).join(" and ")} —
                not set on this definition.
              </span>
            )}
          </div>
        )}
      </Card>

      {/* Members */}
      <Card className="overflow-hidden">
        <div className="flex items-center gap-2 border-b border-line bg-panel2/50 px-4 py-3">
          <Layers className="h-4 w-4 text-faint" />
          <h3 className="text-[13px] font-bold uppercase tracking-wide">Measures</h3>
          <Badge variant="brand">{groups.length}</Badge>
          <div className="ml-auto flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onAssign}>
              <Plus /> Assign measures
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                if (confirm(`Delete definition "${definition.name}"? Its measures keep their metadata.`)) {
                  onDelete();
                }
              }}
            >
              <Trash2 /> Delete
            </Button>
          </div>
        </div>
        {groupsQ.isLoading ? (
          <LoadingState label="Loading measures…" />
        ) : groups.length === 0 ? (
          <EmptyState title="No measures assigned" hint="Use “Assign measures” to add some." />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH className="min-w-[240px]">Measure</TH>
                <TH>Status</TH>
                <TH>Owner</TH>
                <TH>Department</TH>
                <TH className="text-right">Action</TH>
              </TR>
            </THead>
            <TBody>
              {groups.map((g) => (
                <TR key={g.id}>
                  <TD className="font-medium">{g.name}</TD>
                  <TD>{statusLabel(g.status)}</TD>
                  <TD>{g.ownership_person_name ?? <span className="text-faint">—</span>}</TD>
                  <TD>{g.ownership_department_name ?? <span className="text-faint">—</span>}</TD>
                  <TD className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={unassignMut.isPending}
                      onClick={() => unassignMut.mutate(g.id)}
                    >
                      <X /> Remove
                    </Button>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <label className="mb-1 block text-[11px] font-medium text-faint">{label}</label>
      {children}
    </div>
  );
}

function CreateDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: (d: Definition) => void;
}) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const mut = useMutation({
    mutationFn: () => api.definitions.create({ name: name.trim() }),
    onSuccess: (d) => {
      setName("");
      setError(null);
      onOpenChange(false);
      onCreated(d);
    },
    onError: (e) => setError(getApiErrorMessage(e, "Could not create the definition.")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New definition</DialogTitle>
          <DialogDescription>
            A business concept that several measures express — “Revenue”, “Active customers”.
          </DialogDescription>
        </DialogHeader>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Definition name"
          className="h-9 w-full rounded-md border border-input bg-panel px-3 text-[13px] outline-none placeholder:text-faint"
        />
        {error && <div className="text-[12px] text-err">{error}</div>}
        <div className="flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="brand"
            size="sm"
            disabled={!name.trim() || mut.isPending}
            onClick={() => mut.mutate()}
          >
            Create
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Pick measure groups to add. Searches the measure catalogue by name; a group
 *  already in another definition is shown with that definition so moving it is
 *  a deliberate choice rather than a surprise. */
function AssignDialog({
  open,
  onOpenChange,
  definition,
  onDone,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  definition: Definition;
  onDone: () => void;
}) {
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<Set<number>>(new Set());

  const searchQ = useQuery({
    queryKey: ["definition-assign-search", q],
    queryFn: () =>
      api.items.list({
        item_type: "PB_MEASURE",
        search: q.trim() || undefined,
        limit: 200,
        ordering: "item_name",
      }),
    enabled: open,
  });

  // One row per measure group, not per instance — the group is what gets assigned.
  const candidates = useMemo(() => {
    const rows = unwrapResults<Item>(searchQ.data);
    const byGroup = new Map<number, { id: number; name: string; definition?: string | null }>();
    for (const r of rows) {
      const gid = r.group;
      if (!gid || byGroup.has(gid)) continue;
      byGroup.set(gid, {
        id: gid,
        name: r.item_name,
        definition: (r as unknown as { definition_name?: string | null }).definition_name ?? null,
      });
    }
    return [...byGroup.values()];
  }, [searchQ.data]);

  const mut = useMutation({
    mutationFn: () => api.definitions.assign(definition.id, { add: [...picked] }),
    onSuccess: () => {
      setPicked(new Set());
      onOpenChange(false);
      onDone();
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Assign measures to “{definition.name}”</DialogTitle>
          <DialogDescription>
            Membership only — this does not change any measure&apos;s owner or department. Use the
            action afterwards if you want to push them down.
          </DialogDescription>
        </DialogHeader>

        <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-panel px-3 text-[13px]">
          <Search className="h-3.5 w-3.5 text-faint" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search measures…"
            className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-faint"
          />
        </div>

        <div className="max-h-[45vh] overflow-y-auto rounded-md border border-line">
          {searchQ.isLoading ? (
            <LoadingState label="Searching…" />
          ) : candidates.length === 0 ? (
            <EmptyState title="No measures found" />
          ) : (
            candidates.map((c) => (
              <label
                key={c.id}
                className="flex cursor-pointer items-center gap-2.5 border-b border-line px-3 py-2 text-[13px] last:border-b-0 hover:bg-panel2"
              >
                <input
                  type="checkbox"
                  checked={picked.has(c.id)}
                  onChange={() =>
                    setPicked((prev) => {
                      const next = new Set(prev);
                      if (next.has(c.id)) next.delete(c.id);
                      else next.add(c.id);
                      return next;
                    })
                  }
                  className="h-3.5 w-3.5 accent-[oklch(var(--welcome-teal))]"
                />
                <span className="min-w-0 flex-1 truncate">{c.name}</span>
                {c.definition && (
                  <Badge variant="outline" title="Currently in another definition — assigning moves it">
                    {c.definition}
                  </Badge>
                )}
              </label>
            ))
          )}
        </div>

        <div className="flex items-center justify-between gap-2">
          <span className="text-[12px] text-faint">{picked.size} selected</span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              variant="brand"
              size="sm"
              disabled={picked.size === 0 || mut.isPending}
              onClick={() => mut.mutate()}
            >
              Assign {picked.size > 0 ? picked.size : ""}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
