"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownToLine, Layers, Plus, Search, Trash2, X } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import { useDebounced } from "@/lib/use-debounced";
import { buildDefinitionCandidates } from "@/app/definitions/definition-assignment";
import {
  buildDefinitionCommitRequest,
  buildDefinitionPreviewRequest,
  definitionApplyContextKey,
  definitionApplyPreviewAuthorizes,
  definitionApplyResponseIsCurrent,
  normalizeDefinitionApplyFields,
} from "@/app/definitions/definition-apply";
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
  type DefinitionApplyField,
  type DefinitionApplyResult,
  type Department,
} from "@/lib/api";

export default function DefinitionsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [assignOpen, setAssignOpen] = useState(false);
  const [definitionPreviewVersion, setDefinitionPreviewVersion] = useState(0);
  const [note, setNote] = useState<string | null>(null);

  const defsQ = useQuery({
    queryKey: ["definitions"],
    queryFn: () => api.definitions.listAll({ limit: 1000 }),
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

  const definitions = defsQ.data ?? [];
  const people = metaQ.data?.people ?? [];
  const departments = metaQ.data?.departments ?? [];
  const selected = definitions.find((d) => d.id === selectedId) ?? null;

  function invalidateDefinitionPreview() {
    setDefinitionPreviewVersion((version) => version + 1);
  }

  function handleAssignOpenChange(next: boolean) {
    if (!next) invalidateDefinitionPreview();
    setAssignOpen(next);
  }

  function refresh() {
    qc.invalidateQueries({ queryKey: ["definitions"] });
    qc.invalidateQueries({ queryKey: ["definition-groups"] });
    qc.invalidateQueries({ queryKey: ["definition-assign-search"] });
    qc.invalidateQueries({ queryKey: ["dict-meta"] });
    qc.invalidateQueries({ queryKey: ["dict-items"] });
    qc.invalidateQueries({ queryKey: ["dashboard"] });
    qc.invalidateQueries({ queryKey: ["tasks"] });
    qc.invalidateQueries({ queryKey: ["tasks-summary"] });
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
    onError: (e) => setNote(getApiErrorMessage(e, "Could not delete the definition.")),
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
      {defsQ.isError && (
        <Card>
          <EmptyState
            title="Could not load definitions"
            hint={getApiErrorMessage(defsQ.error, "Refresh the page to try again.")}
          />
        </Card>
      )}
      {metaQ.isError && (
        <div className="mb-4 rounded-md border border-err/40 bg-err/5 px-3 py-2 text-[12px] text-err" role="alert">
          {getApiErrorMessage(metaQ.error, "Could not load owners and departments.")}
        </div>
      )}
      {!defsQ.isLoading && !defsQ.isError && definitions.length === 0 && (
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
              metadataPending={saveMut.isPending}
              previewResetVersion={definitionPreviewVersion}
              onSave={(body) => saveMut.mutate({ id: selected.id, body })}
              onDelete={() => deleteMut.mutate(selected.id)}
              onAssign={() => {
                invalidateDefinitionPreview();
                setAssignOpen(true);
              }}
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
          key={selected.id}
          open={assignOpen}
          onOpenChange={handleAssignOpenChange}
          definition={selected}
          onDone={refresh}
          onError={invalidateDefinitionPreview}
        />
      )}
    </div>
  );
}

function DefinitionDetail({
  definition,
  people,
  departments,
  metadataPending,
  previewResetVersion,
  onSave,
  onDelete,
  onAssign,
  onApplied,
}: {
  definition: Definition;
  people: DataPerson[];
  departments: Department[];
  metadataPending: boolean;
  previewResetVersion: number;
  onSave: (body: Partial<Definition>) => void;
  onDelete: () => void;
  onAssign: () => void;
  onApplied: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const [preview, setPreview] = useState<DefinitionApplyResult | null>(null);
  const [previewSignature, setPreviewSignature] = useState<string | null>(null);
  const [previewEpoch, setPreviewEpoch] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [applyFields, setApplyFields] = useState<DefinitionApplyField[]>([
    "ownership_person",
    "ownership_department",
  ]);
  const requestEpochRef = useRef(0);

  const groupsQ = useQuery({
    queryKey: ["definition-groups", definition.id],
    queryFn: () => api.definitions.groups(definition.id),
  });
  const groups = groupsQ.data ?? [];
  const applyContextKey = definitionApplyContextKey({
    definitionId: definition.id,
    ownerId: definition.ownership_person ?? null,
    departmentId: definition.ownership_department ?? null,
    fields: applyFields,
    groupIds: groups.map((group) => group.id),
  });
  const applyGuardKey = `${previewResetVersion}:${applyContextKey}`;
  const currentGuardKeyRef = useRef(applyGuardKey);
  if (currentGuardKeyRef.current !== applyGuardKey) {
    currentGuardKeyRef.current = applyGuardKey;
    requestEpochRef.current += 1;
  }

  function invalidatePreview(clearError = true) {
    requestEpochRef.current += 1;
    setPreview(null);
    setPreviewSignature(null);
    setPreviewEpoch(null);
    if (clearError) setActionError(null);
  }

  const previewMatches =
    definitionApplyPreviewAuthorizes({
      dryRun: preview?.dry_run,
      previewKey: previewSignature,
      currentKey: applyGuardKey,
      previewToken: preview?.preview_token,
    }) && previewEpoch === requestEpochRef.current;

  type ApplyRequest =
    | {
        mode: "preview";
        signature: string;
        epoch: number;
        fields: DefinitionApplyField[];
      }
    | {
        mode: "commit";
        signature: string;
        epoch: number;
        fields: DefinitionApplyField[];
        previewToken: string;
      };

  const applyMut = useMutation({
    mutationFn: (request: ApplyRequest) =>
      api.definitions.apply(
        definition.id,
        request.mode === "preview"
          ? buildDefinitionPreviewRequest(request.fields)
          : buildDefinitionCommitRequest(request.fields, request.previewToken),
      ),
    onSuccess: (res, request) => {
      if (request.mode === "commit") {
        invalidatePreview();
        qc.invalidateQueries({ queryKey: ["definition-groups", definition.id] });
        onApplied(
          (res.updated ?? 0) === 0
            ? "Nothing to change — every measure already matches."
            : `Selected metadata applied to ${res.updated} measure group${res.updated === 1 ? "" : "s"}.`,
        );
        return;
      }
      if (
        !definitionApplyResponseIsCurrent({
          requestKey: request.signature,
          currentKey: currentGuardKeyRef.current,
          requestEpoch: request.epoch,
          currentEpoch: requestEpochRef.current,
        })
      ) {
        return;
      }
      setActionError(null);
      if (res.dry_run !== true || !res.preview_token) {
        invalidatePreview(false);
        setActionError(
          "The server did not authorize this preview. Preview again after the deployment is complete.",
        );
        return;
      }
      setPreview(res);
      setPreviewSignature(request.signature);
      setPreviewEpoch(request.epoch);
    },
    onError: (e, request) => {
      if (
        !definitionApplyResponseIsCurrent({
          requestKey: request.signature,
          currentKey: currentGuardKeyRef.current,
          requestEpoch: request.epoch,
          currentEpoch: requestEpochRef.current,
        })
      ) {
        return;
      }
      invalidatePreview(false);
      setActionError(getApiErrorMessage(e, "Could not apply definition metadata."));
    },
  });
  const unassignMut = useMutation({
    mutationFn: (groupId: number) => api.definitions.assign(definition.id, { remove: [groupId] }),
    onSuccess: () => {
      invalidatePreview();
      qc.invalidateQueries({ queryKey: ["definition-groups", definition.id] });
      qc.invalidateQueries({ queryKey: ["definitions"] });
      qc.invalidateQueries({ queryKey: ["dict-items"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["tasks-summary"] });
    },
    onError: (e) => {
      invalidatePreview(false);
      setActionError(getApiErrorMessage(e, "Could not remove this measure."));
    },
  });

  const normalizedFields = normalizeDefinitionApplyFields(applyFields);
  const hasSelectedValue = normalizedFields.some((field) =>
    field === "ownership_person"
      ? !!definition.ownership_person
      : !!definition.ownership_department,
  );
  const canApply =
    groupsQ.isSuccess &&
    normalizedFields.length > 0 &&
    hasSelectedValue &&
    !metadataPending;
  const applyBlocker = metadataPending
    ? "Wait for the owner or department change to finish before previewing."
    : !groupsQ.isSuccess
      ? "The exact measure membership must load before previewing."
      : normalizedFields.length === 0
        ? "Choose at least one field to apply."
        : !hasSelectedValue
          ? "Set a value for at least one selected field before previewing."
          : null;

  function toggleApplyField(field: DefinitionApplyField) {
    invalidatePreview();
    setApplyFields((current) =>
      current.includes(field)
        ? current.filter((candidate) => candidate !== field)
        : [...current, field],
    );
  }

  function startPreview() {
    const epoch = requestEpochRef.current + 1;
    requestEpochRef.current = epoch;
    setPreview(null);
    setPreviewSignature(null);
    setPreviewEpoch(null);
    setActionError(null);
    applyMut.mutate({
      mode: "preview",
      signature: currentGuardKeyRef.current,
      epoch,
      fields: normalizeDefinitionApplyFields(applyFields),
    });
  }

  function commitPreview() {
    if (!previewMatches || !preview?.preview_token) return;
    setActionError(null);
    applyMut.mutate({
      mode: "commit",
      signature: currentGuardKeyRef.current,
      epoch: requestEpochRef.current,
      fields: normalizeDefinitionApplyFields(applyFields),
      previewToken: preview.preview_token,
    });
  }

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
              disabled={metadataPending}
              onValueChange={(v) => {
                invalidatePreview();
                onSave({ ownership_person: v ? Number(v) : null });
              }}
              options={[
                { value: "", label: "— none —" },
                ...people.map((p) => ({ value: String(p.id), label: p.name })),
              ]}
            />
          </Field>
          <Field label="Department">
            <SimpleSelect
              value={definition.ownership_department ? String(definition.ownership_department) : ""}
              disabled={metadataPending}
              onValueChange={(v) => {
                invalidatePreview();
                onSave({ ownership_department: v ? Number(v) : null });
              }}
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
              Writes the selected metadata onto all {groups.length} assigned measure group
              {groups.length === 1 ? "" : "s"}, replacing what they
              have. Fields left empty here are skipped, not cleared.
            </div>
            <div className="mt-2 flex flex-wrap gap-3 text-[12px]">
              <label className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={applyFields.includes("ownership_person")}
                  disabled={applyMut.isPending}
                  onChange={() => toggleApplyField("ownership_person")}
                  className="h-3.5 w-3.5 accent-[oklch(var(--welcome-teal))]"
                />
                Owner
              </label>
              <label className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={applyFields.includes("ownership_department")}
                  disabled={applyMut.isPending}
                  onChange={() => toggleApplyField("ownership_department")}
                  className="h-3.5 w-3.5 accent-[oklch(var(--welcome-teal))]"
                />
                Department
              </label>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            disabled={!canApply || applyMut.isPending}
            onClick={startPreview}
          >
            Preview
          </Button>
          <Button
            variant="brand"
            size="sm"
            disabled={!canApply || applyMut.isPending || !previewMatches}
            onClick={commitPreview}
          >
            <ArrowDownToLine /> Apply
          </Button>
        </div>
        {applyBlocker && (
          <div className="border-t border-line px-4 py-2 text-[11px] text-warn">
            {applyBlocker}
          </div>
        )}
        {canApply && !previewMatches && (
          <div className="border-t border-line px-4 py-2 text-[11px] text-faint">
            Preview the current owner, department, and membership before applying.
          </div>
        )}
        {actionError && (
          <div className="border-t border-err/30 bg-err/5 px-4 py-2 text-[12px] text-err" role="alert">
            {actionError}
          </div>
        )}
        {preview && previewMatches && (
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
            <Button
              variant="outline"
              size="sm"
              disabled={applyMut.isPending}
              onClick={onAssign}
            >
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
        ) : groupsQ.isError ? (
          <EmptyState
            title="Could not load assigned measures"
            hint={getApiErrorMessage(groupsQ.error, "Refresh the page to try again.")}
          />
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
                      disabled={unassignMut.isPending || applyMut.isPending}
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

  function handleOpenChange(next: boolean) {
    if (!next) {
      setName("");
      setError(null);
      mut.reset();
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
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
          <Button variant="outline" size="sm" onClick={() => handleOpenChange(false)}>
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
  onError,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  definition: Definition;
  onDone: () => void;
  onError: () => void;
}) {
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const search = useDebounced(q, 300);

  useEffect(() => {
    setQ("");
    setPicked(new Set());
    setError(null);
  }, [definition.id]);

  const searchQ = useQuery({
    queryKey: ["definition-assign-search", search],
    queryFn: () =>
      api.items.listAll({
        item_type: "PB_MEASURE",
        search: search.trim() || undefined,
        limit: 1000,
        ordering: "item_name",
      }),
    enabled: open,
  });

  // One row per measure group, not per instance — the group is what gets assigned.
  const candidates = useMemo(() => {
    return buildDefinitionCandidates(searchQ.data ?? []);
  }, [searchQ.data]);

  const mut = useMutation({
    mutationFn: () => api.definitions.assign(definition.id, { add: [...picked] }),
    onSuccess: () => {
      setPicked(new Set());
      setQ("");
      setError(null);
      onOpenChange(false);
      onDone();
    },
    onError: (e) => {
      onError();
      setError(getApiErrorMessage(e, "Could not assign the selected measures."));
    },
  });

  function handleOpenChange(next: boolean) {
    if (!next) {
      setQ("");
      setPicked(new Set());
      setError(null);
      mut.reset();
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
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
          ) : searchQ.isError ? (
            <EmptyState
              title="Could not search measures"
              hint={getApiErrorMessage(searchQ.error, "Try again.")}
            />
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
        <div className="text-[11px] text-faint">
          {searchQ.isFetching
            ? "Loading all matching measure groups…"
            : `${candidates.length.toLocaleString()} matching measure group${candidates.length === 1 ? "" : "s"}`}
        </div>
        {error && (
          <div className="rounded-md border border-err/40 bg-err/5 px-3 py-2 text-[12px] text-err" role="alert">
            {error}
          </div>
        )}

        <div className="flex items-center justify-between gap-2">
          <span className="text-[12px] text-faint">{picked.size} selected</span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => handleOpenChange(false)}>
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
