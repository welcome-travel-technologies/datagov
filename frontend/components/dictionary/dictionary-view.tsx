"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  Download,
  GitBranch,
  Link2,
  Plus,
  Upload,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/simple-select";
import {
  api,
  unwrapResults,
  type Category,
  type DataPerson,
  type Department,
  type Item,
  type ItemStatus,
} from "@/lib/api";
import { GROUP_LABELS, colorFor } from "@/lib/lineage/graph-utils";
import { buildGroups, type GroupedItem } from "@/components/dictionary/grouping";
import { DetailsModal } from "@/components/dictionary/details-modal";

const TYPE_OPTIONS: { label: string; options: { value: string; label: string }[] }[] = [
  {
    label: "PowerBI",
    options: [
      { value: "ALL_PB", label: "All PowerBI" },
      { value: "PB_MEASURE", label: "PB Measures" },
      { value: "PB_COLUMN", label: "PB Columns" },
      { value: "PB_TABLE", label: "PB Tables" },
      { value: "PB_WORKSPACE", label: "PB Workspaces" },
      { value: "PB_REPORT", label: "PB Reports" },
      { value: "PB_PAGE", label: "PB Pages" },
      { value: "PB_VISUAL", label: "PB Visuals" },
      { value: "PB_FIELD", label: "PB Fields" },
    ],
  },
  {
    label: "dbt",
    options: [
      { value: "ALL_DBT", label: "All dbt" },
      { value: "DBT_MODEL", label: "dbt Models" },
      { value: "DBT_SOURCE", label: "dbt Sources" },
      { value: "DBT_SEED", label: "dbt Seeds" },
      { value: "DBT_TEST", label: "dbt Tests" },
      { value: "DBT_COLUMN", label: "dbt Columns" },
    ],
  },
];

const STATUS_LABELS: Record<ItemStatus, string> = {
  UNVERIFIED: "Unverified",
  VERIFIED: "Verified",
  DELETED: "Deleted",
  ATTENTION: "Attention",
};

const PAGE_SIZE = 25;

const CELL_SELECT_CLS = "h-7 px-1.5 text-[12px]";

function statusCls(s?: ItemStatus | null): string {
  switch (s) {
    case "VERIFIED":
      return "border-ok/30 bg-ok/10 text-ok";
    case "DELETED":
      return "border-err/30 bg-err/10 text-err";
    case "ATTENTION":
      return "border-warn/30 bg-warn/10 text-warn";
    default:
      return "border-line-strong bg-panel text-muted-foreground";
  }
}

/** '' = all, 'none' = unassigned, otherwise the id must match. */
function govIdMatch(sel: string, val: number | null | undefined): boolean {
  if (!sel) return true;
  if (sel === "none") return !val;
  return String(val ?? "") === sel;
}

export function DictionaryView() {
  // ---- filter state -------------------------------------------------------
  const [itemType, setItemType] = useState("PB_MEASURE");
  const [ws, setWs] = useState("");
  const [ds, setDs] = useState("");
  const [tbl, setTbl] = useState("");
  const [searchName, setSearchName] = useState("");
  const [searchMode, setSearchMode] = useState<"contains" | "exact" | "regex">("contains");
  const [noDesc, setNoDesc] = useState("");
  const [used, setUsed] = useState("");
  const [statusF, setStatusF] = useState("");
  const [sharing, setSharing] = useState("");
  const [dept, setDept] = useState("");
  const [owner, setOwner] = useState("");
  const [steward, setSteward] = useState("");
  const [category, setCategory] = useState("");
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<GroupedItem | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const resetPage = () => setPage(1);

  // ---- data ---------------------------------------------------------------
  const itemsQ = useQuery({
    queryKey: ["dict-items", itemType],
    queryFn: async () => {
      const params: Record<string, string | number> = { limit: 100000 };
      if (itemType === "ALL_PB") params.service = "powerbi";
      else if (itemType === "ALL_DBT") params.service = "dbt";
      else params.item_type = itemType;
      const res = await api.items.list(params);
      return res.results;
    },
    staleTime: 60_000,
  });

  // Cascading filter options: datasets narrow to the chosen workspace, tables
  // narrow to the chosen workspace + dataset.
  const filtersQ = useQuery({
    queryKey: ["dict-filters", ws, ds],
    queryFn: () => api.filters({ workspace_name: ws, dataset_name: ds }),
    staleTime: 5 * 60_000,
  });
  const metaQ = useQuery({
    queryKey: ["dict-meta"],
    queryFn: async () => {
      const [d, o, s, c] = await Promise.all([
        api.departments.list(),
        api.dataPersons.list({ is_owner: true }),
        api.dataPersons.list({ is_steward: true }),
        api.categories.list(),
      ]);
      return {
        departments: unwrapResults<Department>(d),
        owners: unwrapResults<DataPerson>(o),
        stewards: unwrapResults<DataPerson>(s),
        categories: unwrapResults<Category>(c),
      };
    },
    staleTime: 5 * 60_000,
  });

  const departments = metaQ.data?.departments ?? [];
  const owners = metaQ.data?.owners ?? [];
  const stewards = metaQ.data?.stewards ?? [];
  const categories = metaQ.data?.categories ?? [];

  // Local working copy so inline edits update without refetching 100k rows.
  const [rawRows, setRawRows] = useState<Item[]>([]);
  useEffect(() => {
    setRawRows(itemsQ.data ?? []);
  }, [itemsQ.data]);

  // ---- grouping + filtering ----------------------------------------------
  const groups = useMemo(() => buildGroups(rawRows, { ws, ds, tbl }), [rawRows, ws, ds, tbl]);

  const regexInvalid = useMemo(() => {
    if (searchMode !== "regex" || !searchName) return false;
    try {
      new RegExp(searchName);
      return false;
    } catch {
      return true;
    }
  }, [searchMode, searchName]);

  const filtered = useMemo(() => {
    const nameQ = searchName.trim();
    let nameRe: RegExp | null = null;
    if (nameQ && searchMode === "regex" && !regexInvalid) {
      try {
        nameRe = new RegExp(nameQ, "i");
      } catch {
        nameRe = null;
      }
    }
    return groups.filter((row) => {
      // Name search
      if (nameQ) {
        const nm = row.item_name || "";
        if (searchMode === "exact") {
          if (nm.toLowerCase() !== nameQ.toLowerCase()) return false;
        } else if (searchMode === "regex") {
          if (nameRe && !nameRe.test(nm)) return false;
        } else if (nm.toLowerCase().indexOf(nameQ.toLowerCase()) === -1) return false;
      }
      if (row._loc_match === false) return false;

      // In Use — any instance counts
      const insts = row._instances;
      if (used === "yes" && !insts.some((x) => x.is_used === true)) return false;
      if (used === "no" && !insts.some((x) => x.is_used === false)) return false;

      // Documentation
      if (noDesc) {
        const hasNoDesc = !(row.description || "").trim() && !(row.custom_description || "").trim();
        if (noDesc === "yes" && !hasNoDesc) return false;
        if (noDesc === "no" && hasNoDesc) return false;
      }

      // Status
      if (statusF && (row.status || "UNVERIFIED") !== statusF) return false;

      // Governance
      if (!govIdMatch(dept, row.ownership_department)) return false;
      if (!govIdMatch(owner, row.ownership_person)) return false;
      if (!govIdMatch(steward, row.steward)) return false;
      if (!govIdMatch(category, row.category)) return false;

      // Sharing — only meaningful for measure_name groups
      if (sharing && row.group_kind === "measure_name") {
        if (sharing === "multi" && !row._is_group) return false;
        if (sharing === "single" && row._is_group) return false;
      }
      return true;
    });
  }, [
    groups,
    searchName,
    searchMode,
    regexInvalid,
    used,
    noDesc,
    statusF,
    dept,
    owner,
    steward,
    category,
    sharing,
  ]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  // Dept-narrowed option lists for the FILTER dropdowns (cascade).
  const filterOwners = useMemo(() => narrowByDept(owners, dept), [owners, dept]);
  const filterStewards = useMemo(() => narrowByDept(stewards, dept), [stewards, dept]);

  // ---- mutations ----------------------------------------------------------
  function applyToGroup(groupPk: number | null | undefined, patch: Partial<Item>) {
    if (groupPk == null) return;
    setRawRows((prev) => prev.map((r) => (r.group === groupPk ? { ...r, ...patch } : r)));
  }

  async function patchGroup(groupPk: number | null | undefined, body: Record<string, unknown>, local: Partial<Item>) {
    if (groupPk == null) {
      alert("No group found for this row.");
      return;
    }
    // Empty strings clear the FK to null.
    const clean: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(body)) clean[k] = v === "" ? null : v;
    applyToGroup(groupPk, local);
    try {
      await api.itemGroups.patch(groupPk, clean);
    } catch {
      alert("Error updating field. You may not have permission.");
      itemsQ.refetch();
    }
  }

  function editCategory(row: GroupedItem, value: string) {
    const cat = categories.find((c) => String(c.id) === value);
    patchGroup(row.group, { category: value }, {
      category: value ? Number(value) : null,
      category_name: cat?.name ?? null,
    });
  }
  function editStatus(row: GroupedItem, value: ItemStatus) {
    patchGroup(row.group, { status: value }, { status: value });
  }
  function editOwner(row: GroupedItem, value: string) {
    const p = owners.find((o) => String(o.id) === value);
    patchGroup(row.group, { ownership_person: value }, {
      ownership_person: value ? Number(value) : null,
      ownership_person_name: p?.name ?? null,
      ownership_person_slack: p?.slack_handle ?? null,
    });
  }
  function editSteward(row: GroupedItem, value: string) {
    const p = stewards.find((s) => String(s.id) === value);
    patchGroup(row.group, { steward: value }, {
      steward: value ? Number(value) : null,
      steward_name: p?.name ?? null,
      steward_slack: p?.slack_handle ?? null,
    });
  }
  function editDept(row: GroupedItem, value: string) {
    const d = departments.find((x) => String(x.id) === value);
    // Changing dept clears owner + steward (the new pool is unrelated).
    patchGroup(
      row.group,
      { ownership_department: value, ownership_person: null, steward: null },
      {
        ownership_department: value ? Number(value) : null,
        ownership_department_name: d?.name ?? null,
        ownership_person: null,
        ownership_person_name: null,
        steward: null,
        steward_name: null,
      },
    );
  }
  function editAnnotation(row: GroupedItem, value: string) {
    if (value === (row.custom_description || "")) return;
    patchGroup(row.group, { custom_description: value }, { custom_description: value });
  }

  async function setPrimary(itemId: string) {
    try {
      await api.items.setPrimary(itemId);
      const chosen = rawRows.find((r) => r.item_id === itemId);
      const gpk = chosen?.group ?? null;
      setRawRows((prev) =>
        prev.map((r) => (gpk != null && r.group === gpk ? { ...r, is_primary: r.item_id === itemId } : r)),
      );
      // Re-open Details on the new representative.
      const rebuilt = buildGroups(
        rawRows.map((r) => (gpk != null && r.group === gpk ? { ...r, is_primary: r.item_id === itemId } : r)),
        { ws, ds, tbl },
      );
      const next = rebuilt.find((g) => g.item_id === itemId);
      setDetail(next ?? null);
    } catch {
      alert("Could not set primary. You may not have permission.");
    }
  }

  async function onImportCsv(file: File) {
    setBusy(true);
    try {
      const r = await api.governance.importCsv(file);
      let msg = r.message || "Done.";
      const detailLines = (label: string, list: unknown[] | undefined, fmt: (x: never) => string) => {
        if (!list || !list.length) return "";
        const shown = list.slice(0, 10).map((x) => fmt(x as never)).join("\n  ");
        const more = list.length > 10 ? `\n  …and ${list.length - 10} more` : "";
        return `\n\n${label} (${list.length}):\n  ${shown}${more}`;
      };
      msg += detailLines("Unmatched group_id", r.skipped_no_match, (x: { row: number; group_id: string }) => `row ${x.row}: ${x.group_id}`);
      msg += detailLines("Unknown names (skipped)", r.unmatched_values, (x: { row: number; field: string; value: string }) => `row ${x.row}: ${x.field}="${x.value}"`);
      msg += detailLines("Ambiguous names (skipped)", r.ambiguous, (x: { row: number; field: string; value: string }) => `row ${x.row}: ${x.field}="${x.value}"`);
      msg += detailLines("Invalid status (skipped)", r.invalid_status, (x: { row: number; value: string }) => `row ${x.row}: "${x.value}"`);
      alert(msg);
      if ((r.updated ?? 0) > 0) itemsQ.refetch();
    } catch (e) {
      alert((e as Error).message || "Upload failed. Use a file from “Governance CSV”.");
    } finally {
      setBusy(false);
    }
  }

  const loading = itemsQ.isLoading;

  return (
    <div className="space-y-4">
      {/* header actions */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-[13px] text-muted-foreground">
          Searchable dictionary across PowerBI and dbt assets. Pick a type to load.
        </p>
        <div className="flex items-center gap-2">
          <a
            href={api.governance.exportCsvUrl}
            download
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line-strong bg-panel px-3 text-[12px] font-semibold text-foreground transition-colors hover:bg-panel2"
            title="Download governance (owner / steward / status / category / annotation), one row per group"
          >
            <Download className="h-3.5 w-3.5" /> Governance CSV
          </a>
          <button
            type="button"
            disabled={busy}
            onClick={() => fileRef.current?.click()}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line-strong bg-panel px-3 text-[12px] font-semibold text-foreground transition-colors hover:bg-panel2 disabled:opacity-50"
            title="Upload an edited Governance CSV. Rows are matched by group_id; empty cells are left unchanged."
          >
            <Upload className="h-3.5 w-3.5" /> Upload CSV
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onImportCsv(f);
              e.target.value = "";
            }}
          />
        </div>
      </div>

      <Card className="overflow-hidden">
        {/* filter toolbar */}
        <div className="space-y-3 border-b border-line bg-panel2/40 p-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
            <Field label="Type">
              <SimpleSelect
                value={itemType}
                onValueChange={(v) => { setItemType(v); resetPage(); }}
                groups={TYPE_OPTIONS}
              />
            </Field>
            <Field label="Workspace">
              <SimpleSelect
                value={ws}
                onValueChange={(v) => { setWs(v); setDs(""); setTbl(""); resetPage(); }}
                options={[
                  { value: "", label: "All Workspaces" },
                  ...(filtersQ.data?.workspaces ?? []).map((w) => ({ value: w, label: w })),
                ]}
              />
            </Field>
            <Field label="Dataset">
              <SimpleSelect
                value={ds}
                onValueChange={(v) => { setDs(v); setTbl(""); resetPage(); }}
                options={[
                  { value: "", label: "All Datasets" },
                  ...(filtersQ.data?.datasets ?? []).map((d) => ({ value: d, label: d })),
                ]}
              />
            </Field>
            <Field label="Table">
              <SimpleSelect
                value={tbl}
                onValueChange={(v) => { setTbl(v); resetPage(); }}
                options={[
                  { value: "", label: "All Tables" },
                  ...(filtersQ.data?.tables ?? []).map((t) => ({ value: t, label: t })),
                ]}
              />
            </Field>
            <Field label="Name search">
              <div className="flex gap-2">
                <input
                  value={searchName}
                  onChange={(e) => { setSearchName(e.target.value); resetPage(); }}
                  placeholder="Filter by name…"
                  spellCheck={false}
                  className={`h-9 min-w-0 flex-1 rounded-md border bg-panel px-2.5 text-[13px] outline-none focus:ring-1 focus:ring-ring ${regexInvalid ? "border-err ring-1 ring-err/40" : "border-input"}`}
                />
                <SimpleSelect
                  value={searchMode}
                  onValueChange={(v) => { setSearchMode(v as typeof searchMode); resetPage(); }}
                  className="w-[104px] shrink-0 px-2"
                  title="How the name search matches"
                  options={[
                    { value: "contains", label: "Contains" },
                    { value: "exact", label: "Exact" },
                    { value: "regex", label: "Regex" },
                  ]}
                />
              </div>
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
            <Field label="Documentation">
              <SimpleSelect
                value={noDesc}
                onValueChange={(v) => { setNoDesc(v); resetPage(); }}
                options={[
                  { value: "", label: "All" },
                  { value: "no", label: "Has description" },
                  { value: "yes", label: "Missing description" },
                ]}
              />
            </Field>
            <Field label="Usage">
              <SimpleSelect
                value={used}
                onValueChange={(v) => { setUsed(v); resetPage(); }}
                options={[
                  { value: "", label: "All" },
                  { value: "yes", label: "Used" },
                  { value: "no", label: "Unused" },
                ]}
              />
            </Field>
            <Field label="Status">
              <SimpleSelect
                value={statusF}
                onValueChange={(v) => { setStatusF(v); resetPage(); }}
                options={[
                  { value: "", label: "All Statuses" },
                  { value: "UNVERIFIED", label: "Unverified" },
                  { value: "VERIFIED", label: "Verified" },
                  { value: "DELETED", label: "Deleted" },
                  { value: "ATTENTION", label: "Attention" },
                ]}
              />
            </Field>
            <Field label="Sharing">
              <SimpleSelect
                value={sharing}
                onValueChange={(v) => { setSharing(v); resetPage(); }}
                title="Measures whose same name exists in more than one dataset / workspace"
                options={[
                  { value: "", label: "All" },
                  { value: "multi", label: "In multiple datasets/workspaces" },
                  { value: "single", label: "In a single dataset/workspace" },
                ]}
              />
            </Field>
            <Field label="Category">
              <SimpleSelect
                value={category}
                onValueChange={(v) => { setCategory(v); resetPage(); }}
                options={[
                  { value: "", label: "All Categories" },
                  { value: "none", label: "No Category" },
                  ...categories.map((c) => ({ value: String(c.id), label: c.name })),
                ]}
              />
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
            <Field label="Department">
              <SimpleSelect
                value={dept}
                onValueChange={(v) => { setDept(v); setOwner(""); setSteward(""); resetPage(); }}
                options={[
                  { value: "", label: "All Departments" },
                  { value: "none", label: "No Department" },
                  ...departments.map((d) => ({ value: String(d.id), label: d.name })),
                ]}
              />
            </Field>
            <Field label="Owner">
              <SimpleSelect
                value={owner}
                onValueChange={(v) => { setOwner(v); resetPage(); }}
                options={[
                  { value: "", label: "All Owners" },
                  { value: "none", label: "No Owner" },
                  ...filterOwners.map((o) => ({ value: String(o.id), label: o.name })),
                ]}
              />
            </Field>
            <Field label="Steward">
              <SimpleSelect
                value={steward}
                onValueChange={(v) => { setSteward(v); resetPage(); }}
                options={[
                  { value: "", label: "All Stewards" },
                  { value: "none", label: "No Steward" },
                  ...filterStewards.map((s) => ({ value: String(s.id), label: s.name })),
                ]}
              />
            </Field>
          </div>
        </div>

        {/* count + table */}
        <div className="flex items-center justify-between border-b border-line px-4 py-2.5 text-[12px] text-faint">
          <span>{filtered.length.toLocaleString()} rows</span>
          {itemsQ.isFetching && <span>Loading…</span>}
        </div>

        {loading && <LoadingState label="Loading dictionary…" />}
        {!loading && itemsQ.isError && (
          <EmptyState title="Failed to load" hint="The dictionary API returned an error." />
        )}
        {!loading && !itemsQ.isError && filtered.length === 0 && (
          <EmptyState title="No items found" hint="Try a different type or relax the filters." />
        )}

        {!loading && pageRows.length > 0 && (
          <>
            <Table>
              <THead>
                <TR>
                  <TH className="min-w-[260px]">Name / Description</TH>
                  <TH>Type</TH>
                  <TH>Category</TH>
                  <TH>In Use</TH>
                  <TH className="min-w-[160px]">Ownership</TH>
                  <TH>Status</TH>
                  <TH>Workspace</TH>
                  <TH>Dataset</TH>
                  <TH>Table</TH>
                </TR>
              </THead>
              <TBody>
                {pageRows.map((row) => (
                  <DictRow
                    key={row.item_id}
                    row={row}
                    departments={departments}
                    owners={owners}
                    stewards={stewards}
                    categories={categories}
                    onOpenDetails={() => setDetail(row)}
                    onEditCategory={(v) => editCategory(row, v)}
                    onEditStatus={(v) => editStatus(row, v)}
                    onEditDept={(v) => editDept(row, v)}
                    onEditOwner={(v) => editOwner(row, v)}
                    onEditSteward={(v) => editSteward(row, v)}
                    onEditAnnotation={(v) => editAnnotation(row, v)}
                  />
                ))}
              </TBody>
            </Table>

            <div className="flex items-center justify-between border-t border-line px-4 py-2.5 text-[12.5px] text-muted-foreground">
              <span>Page {page} of {totalPages}</span>
              <div className="flex items-center gap-1">
                <button
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  className="inline-flex h-8 items-center gap-1 rounded-md border border-line-strong bg-panel px-2.5 disabled:opacity-40"
                >
                  <ChevronLeft className="h-3.5 w-3.5" /> Prev
                </button>
                <button
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  className="inline-flex h-8 items-center gap-1 rounded-md border border-line-strong bg-panel px-2.5 disabled:opacity-40"
                >
                  Next <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </>
        )}
      </Card>

      <DetailsModal group={detail} onClose={() => setDetail(null)} onSetPrimary={setPrimary} />
    </div>
  );
}

function narrowByDept(pool: DataPerson[], deptId: string): DataPerson[] {
  if (!deptId || deptId === "none") return pool;
  const idNum = Number(deptId);
  return pool.filter((p) => Array.isArray(p.departments) && p.departments.includes(idNum));
}

/** Owner/steward options for an inline cell, filtered to the row's department. */
function ownerOptions(pool: DataPerson[], deptId: number | null | undefined, selectedId: number | null | undefined) {
  if (!deptId) return [] as DataPerson[];
  const filtered = pool.filter((p) => Array.isArray(p.departments) && p.departments.includes(deptId));
  // Keep a saved-but-now-ineligible person visible so it isn't silently dropped.
  if (selectedId && !filtered.some((p) => p.id === selectedId)) {
    const stale = pool.find((p) => p.id === selectedId);
    if (stale) return [...filtered, { ...stale, name: `${stale.name} (other dept)` }];
  }
  return filtered;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <label className="mb-1 block text-[11px] font-medium text-faint">{label}</label>
      {children}
    </div>
  );
}

function DictRow({
  row,
  departments,
  owners,
  stewards,
  categories,
  onOpenDetails,
  onEditCategory,
  onEditStatus,
  onEditDept,
  onEditOwner,
  onEditSteward,
  onEditAnnotation,
}: {
  row: GroupedItem;
  departments: Department[];
  owners: DataPerson[];
  stewards: DataPerson[];
  categories: Category[];
  onOpenDetails: () => void;
  onEditCategory: (v: string) => void;
  onEditStatus: (v: ItemStatus) => void;
  onEditDept: (v: string) => void;
  onEditOwner: (v: string) => void;
  onEditSteward: (v: string) => void;
  onEditAnnotation: (v: string) => void;
}) {
  const [editingAnno, setEditingAnno] = useState(false);
  const compositeNodeId = `${(row.type || row.item_type || "").toString().toUpperCase()}::${row.item_id}`;
  const deptSet = !!row.ownership_department;
  const isMeasure = row.type === "PB_MEASURE" || row.item_type === "PB_MEASURE";

  return (
    <TR>
      {/* Name / Description */}
      <TD className="max-w-[340px] align-top">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[14px] font-semibold">{row.item_name || "—"}</span>
          <Link
            href={`/lineage?node_id=${encodeURIComponent(compositeNodeId)}`}
            target="_blank"
            className="text-faint hover:text-brand"
            title="View in Lineage Graph"
          >
            <GitBranch className="h-3.5 w-3.5" />
          </Link>
          {row.is_related && Array.isArray(row.relationships_json) && row.relationships_json.length > 0 && (
            <span
              className="inline-flex items-center gap-1 rounded border border-warn/30 bg-warn/10 px-1.5 py-px text-[10px] font-bold text-warn"
              title={row.relationships_json
                .map((r) => `${r.cardinality || "?"}→${r.other_cardinality || "?"}  ${r.other_table || ""}${r.other_column ? "." + r.other_column : ""}${r.is_active === false ? " (inactive)" : ""}`)
                .join("\n")}
            >
              <Link2 className="h-2.5 w-2.5" /> {row.relationships_json.length}
            </span>
          )}
          {row._is_group && (
            <span
              className="rounded border border-info/30 bg-info/10 px-1.5 py-px text-[10px] font-bold text-info"
              title={`Same measure found in ${row._group_count} dataset(s) — governance is shared`}
            >
              ▦ {row._group_count} datasets{row._ws_count > 1 ? ` · ${row._ws_count} workspaces` : ""}
            </span>
          )}
          <button
            type="button"
            onClick={onOpenDetails}
            className="rounded-full border border-brand/20 bg-brand/10 px-2 py-px text-[10px] font-bold uppercase tracking-wide text-brand hover:bg-brand/20"
          >
            Details
          </button>
        </div>
        {row.description && (
          <div className="mt-1 text-[12.5px] text-muted-foreground">
            <div className="line-clamp-3">{row.description}</div>
            {row.description.length > 150 && (
              <button
                type="button"
                onClick={onOpenDetails}
                className="mt-0.5 text-[11px] font-semibold text-brand hover:underline"
              >
                Read more
              </button>
            )}
          </div>
        )}
        {/* curated annotation */}
        <div className="mt-1.5">
          {editingAnno ? (
            <textarea
              autoFocus
              defaultValue={row.custom_description || ""}
              onBlur={(e) => {
                onEditAnnotation(e.target.value);
                setEditingAnno(false);
              }}
              placeholder="Type custom annotation…"
              className="min-h-[50px] w-full rounded border border-brand/40 p-1.5 text-[12px] outline-none focus:ring-1 focus:ring-ring"
            />
          ) : row.custom_description ? (
            <div
              onClick={() => setEditingAnno(true)}
              className="cursor-pointer border-l-2 border-line pl-2 text-[11px] italic text-faint hover:text-brand"
              title="Click to edit custom annotation"
            >
              {row.custom_description}
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setEditingAnno(true)}
              className="inline-flex items-center gap-1 rounded border border-line bg-panel2 px-2 py-0.5 text-[10px] font-medium text-muted-foreground hover:border-brand/30 hover:text-brand"
            >
              <Plus className="h-2.5 w-2.5" /> Annotation
            </button>
          )}
        </div>
      </TD>

      {/* Type */}
      <TD className="align-top">
        <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-[12px]">
          <span className="h-2 w-2 rounded-full" style={{ background: colorFor(row.item_type) }} />
          {GROUP_LABELS[row.item_type] ?? row.type ?? row.item_type}
        </span>
      </TD>

      {/* Category (PB_MEASURE only) */}
      <TD className="align-top">
        {isMeasure ? (
          <SimpleSelect
            value={row.category ? String(row.category) : ""}
            onValueChange={onEditCategory}
            className={CELL_SELECT_CLS}
            options={[
              { value: "", label: "-- Select --" },
              ...categories.map((c) => ({ value: String(c.id), label: c.name })),
            ]}
          />
        ) : (
          <span className="text-faint">—</span>
        )}
      </TD>

      {/* In Use */}
      <TD className="align-top">
        {row.is_used ? <Badge variant="success">Yes</Badge> : <Badge>No</Badge>}
      </TD>

      {/* Ownership */}
      <TD className="min-w-[160px] align-top">
        <div className="flex flex-col gap-1">
          <SimpleSelect
            value={row.ownership_department ? String(row.ownership_department) : ""}
            onValueChange={onEditDept}
            className={CELL_SELECT_CLS}
            options={[
              { value: "", label: "-- Dept --" },
              ...departments.map((d) => ({ value: String(d.id), label: d.name })),
            ]}
          />
          <SimpleSelect
            disabled={!deptSet}
            value={row.ownership_person ? String(row.ownership_person) : ""}
            onValueChange={onEditOwner}
            className={CELL_SELECT_CLS}
            options={[
              { value: "", label: deptSet ? "-- Owner --" : "-- dept first --" },
              ...ownerOptions(owners, row.ownership_department, row.ownership_person).map((o) => ({
                value: String(o.id),
                label: o.slack_handle ? `${o.name} (${o.slack_handle})` : o.name,
              })),
            ]}
          />
          <SimpleSelect
            disabled={!deptSet}
            value={row.steward ? String(row.steward) : ""}
            onValueChange={onEditSteward}
            className={CELL_SELECT_CLS}
            options={[
              { value: "", label: deptSet ? "-- Steward --" : "-- dept first --" },
              ...ownerOptions(stewards, row.ownership_department, row.steward).map((o) => ({
                value: String(o.id),
                label: o.slack_handle ? `${o.name} (${o.slack_handle})` : o.name,
              })),
            ]}
          />
        </div>
      </TD>

      {/* Status */}
      <TD className="align-top">
        <SimpleSelect
          value={row.status || "UNVERIFIED"}
          onValueChange={(v) => onEditStatus(v as ItemStatus)}
          className={`h-7 px-2 text-[11px] font-bold uppercase tracking-wide ${statusCls(row.status)}`}
          options={(Object.keys(STATUS_LABELS) as ItemStatus[]).map((s) => ({
            value: s,
            label: STATUS_LABELS[s],
          }))}
        />
      </TD>

      {/* Workspace */}
      <TD className="max-w-[170px] align-top">
        <div className="truncate text-[12.5px]" title={row.workspace_name || "—"}>
          {row.workspace_name || "—"}
          {row._is_group && row._ws_count > 1 && (
            <span className="ml-1 text-[10px] font-semibold text-faint">+{row._ws_count - 1}</span>
          )}
        </div>
      </TD>

      {/* Dataset */}
      <TD className="max-w-[170px] align-top">
        <div className="truncate text-[12.5px]" title={row.dataset_name || "—"}>
          {row.dataset_name || "—"}
          {row._is_group && row._ds_count > 1 && (
            <span className="ml-1 text-[10px] font-semibold text-faint">+{row._ds_count - 1}</span>
          )}
        </div>
      </TD>

      {/* Table */}
      <TD className="max-w-[200px] align-top">
        <div className="truncate text-[12.5px]" title={row.table_name || "—"}>{row.table_name || "—"}</div>
      </TD>
    </TR>
  );
}
