"use client";

import { useEffect, useState } from "react";
import { Check, Copy, ExternalLink } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { LoadingState } from "@/components/ui/misc";
import { CodeBlock, type CodeLang } from "@/components/ui/code-block";
import { DaxModal } from "@/components/items/dax-modal";
import { api, type ConnectedReport, type Item, type ItemStatus } from "@/lib/api";
import { cn, fmtInt } from "@/lib/utils";
import { isExternalMeasure, sortInstances } from "@/lib/items";
import { detectModelType, MODEL_TYPE_META } from "@/lib/lineage/colibri";

const DBT_TYPES = new Set(["DBT_MODEL", "DBT_SOURCE", "DBT_SEED", "DBT_TEST", "DBT_COLUMN"]);

/** Badge color for a node/item type. Shared by every detail surface. */
export function badgeVariant(t?: string): "default" | "info" | "success" | "brand" | "warning" | "danger" {
  switch (t) {
    case "PB_MEASURE":
      return "success";
    case "PB_TABLE":
      return "info";
    case "PB_REPORT":
    case "DBT_MODEL":
      return "warning";
    case "PB_COLUMN":
    case "DBT_COLUMN":
      return "brand";
    default:
      return "default";
  }
}

function statusVariant(s?: ItemStatus | null): "default" | "success" | "warning" | "danger" {
  switch (s) {
    case "VERIFIED":
      return "success";
    case "DELETED":
      return "danger";
    case "ATTENTION":
      return "warning";
    default:
      return "default";
  }
}

function statusTextClass(s?: ItemStatus | null): string {
  switch (s) {
    case "VERIFIED":
      return "text-ok";
    case "DELETED":
      return "text-err";
    case "ATTENTION":
      return "text-warn";
    default:
      return "text-muted-foreground";
  }
}

/**
 * Fetch the catalog `Item` backing a node: try the id-hash endpoint first, then
 * fall back to a name search. Shared by the lineage detail modal and the
 * metrics-map properties panel so both surface identical characteristics.
 */
export function useItemDetail(idHash: string | null, label: string) {
  const [item, setItem] = useState<Item | null>(null);
  const [loading, setLoading] = useState(false);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    if (!idHash) return;
    let cancelled = false;
    setLoading(true);
    setItem(null);
    setNotFound(false);
    (async () => {
      try {
        const it = await api.items.get(idHash);
        if (!cancelled && it && it.item_id) {
          setItem(it);
          return;
        }
        throw new Error("no item");
      } catch {
        // fall back to a name search
        try {
          const data = await api.items.byName(label);
          if (!cancelled) {
            if (data.results && data.results.length > 0) setItem(data.results[0]);
            else setNotFound(true);
          }
        } catch {
          if (!cancelled) setNotFound(true);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [idHash, label]);

  return { item, loading, notFound };
}

/** Aggregate connected reports across every instance of a group, deduped. */
function aggregateReports(instances: Item[]): ConnectedReport[] {
  const seen = new Set<string>();
  const agg: ConnectedReport[] = [];
  for (const x of instances) {
    for (const rp of x.connected_reports_json ?? []) {
      const k = rp.id || rp.name || "";
      if (k && !seen.has(k)) {
        seen.add(k);
        agg.push(rp);
      }
    }
  }
  agg.sort((a, b) => ((a.name || "") < (b.name || "") ? -1 : 1));
  return agg;
}

/* -------------------------------------------------------------------------- */
/* Modal wrapper — used by lineage (single item) and the catalogue (group).   */
/* -------------------------------------------------------------------------- */

export function ItemDetailModal({
  open,
  onClose,
  item,
  instances,
  loading = false,
  notFound = false,
  title,
  onSetPrimary,
}: {
  open: boolean;
  onClose: () => void;
  item: Item | null;
  /** Group instances; length > 1 surfaces the instances table. */
  instances?: Item[];
  loading?: boolean;
  notFound?: boolean;
  /** Fallback title shown while the item is still loading. */
  title?: string;
  onSetPrimary?: (itemId: string) => void;
}) {
  const insts = instances && instances.length ? instances : item ? [item] : [];
  const isGroup = insts.length > 1;
  const wsCount = new Set(insts.map((x) => x.workspace_name || "")).size;
  const dsCount = new Set(insts.map((x) => x.dataset_name || "")).size;

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle className="truncate pr-8">{item?.item_name || title || "(unnamed)"}</DialogTitle>
          <DialogDescription>
            {item?.type || item?.item_type}
            {isGroup ? (
              <span className="font-semibold text-brand">
                {" · "}
                {insts.length} instances across {wsCount} workspace(s) / {dsCount} dataset(s)
              </span>
            ) : (
              <>
                {item?.workspace_name ? ` · ${item.workspace_name}` : ""}
                {item?.dataset_name ? ` · ${item.dataset_name}` : ""}
              </>
            )}
          </DialogDescription>
          {item && (
            <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
              {item.item_type && <Badge variant={badgeVariant(item.item_type)}>{item.item_type}</Badge>}
              <Badge variant={statusVariant(item.status)}>{item.status || "UNVERIFIED"}</Badge>
            </div>
          )}
        </DialogHeader>

        {loading && <LoadingState label="Fetching details…" />}
        {!loading && notFound && (
          <p className="py-6 text-center text-[13px] text-muted-foreground">
            No detailed characteristics found for this item.
          </p>
        )}
        {!loading && item && <ItemDetail item={item} instances={insts} onSetPrimary={onSetPrimary} />}
      </DialogContent>
    </Dialog>
  );
}

/* -------------------------------------------------------------------------- */
/* The unified body — governance chips, grouped cards, reports, instances,    */
/* and a tabbed code (DAX / SQL / YAML) viewer. Reused by every surface.      */
/* -------------------------------------------------------------------------- */

export function ItemDetail({
  item,
  instances,
  onSetPrimary,
  dense = false,
}: {
  item: Item;
  instances?: Item[];
  onSetPrimary?: (itemId: string) => void;
  dense?: boolean;
}) {
  const [dax, setDax] = useState<{ name: string; expr: string } | null>(null);
  const insts = instances && instances.length ? instances : [item];
  const isGroup = insts.length > 1;

  return (
    <div className="space-y-3">
      <GovChips item={item} />
      <Cards item={item} dense={dense} />
      <Description item={item} />
      <ConnectedReports item={item} instances={insts} />
      {isGroup && (
        <InstancesTable instances={insts} rep={item} onSetPrimary={onSetPrimary} onShowDax={setDax} />
      )}
      <CodeArea item={item} isGroup={isGroup} />
      <DaxModal name={dax?.name ?? null} expr={dax?.expr ?? null} onClose={() => setDax(null)} />
    </div>
  );
}

function GovChips({ item }: { item: Item }) {
  const owner = item.ownership_person_name
    ? item.ownership_person_name + (item.ownership_person_slack ? ` (${item.ownership_person_slack})` : "")
    : "";
  const steward = item.steward_name
    ? item.steward_name + (item.steward_slack ? ` (${item.steward_slack})` : "")
    : "";

  return (
    <div className="flex flex-wrap gap-1.5">
      <Chip k="Status" v={item.status || "UNVERIFIED"} valueClass={statusTextClass(item.status)} />
      {owner && <Chip k="Owner" v={owner} />}
      {steward && <Chip k="Steward" v={steward} />}
      {item.category_name && <Chip k="Category" v={item.category_name} />}
      {item.ownership_department_name && <Chip k="Dept" v={item.ownership_department_name} />}
      {item.organization_name && <Chip k="Org" v={item.organization_name} />}
    </div>
  );
}

function Chip({ k, v, valueClass }: { k: string; v: React.ReactNode; valueClass?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line-strong bg-panel2 px-2.5 py-1 text-[11px]">
      <span className="text-[10px] uppercase tracking-[0.04em] text-faint">{k}</span>
      <span className={cn("font-medium", valueClass)}>{v}</span>
    </span>
  );
}

function Cards({ item, dense }: { item: Item; dense: boolean }) {
  const isDbt = DBT_TYPES.has(String(item.item_type));
  return (
    <div className={cn("grid gap-2.5", dense ? "grid-cols-1" : "grid-cols-2 md:grid-cols-3")}>
      {isDbt && <DbtCard item={item} />}
      <CardBox title="Context">
        <KvRow k="Workspace" v={item.workspace_name} />
        <KvRow k="Dataset" v={item.dataset_name} />
        <KvRow k="Table" v={item.table_name} />
        <KvRow k="Data type" v={item.datatype} />
      </CardBox>
      <CardBox title="Usage">
        <KvRow k="Reports" v={fmtInt(item.connected_reports ?? 0)} />
        <KvRow k="Pages" v={fmtInt(item.connected_report_pages ?? 0)} />
        <KvRow k="Visuals" v={fmtInt(item.connected_visuals ?? 0)} />
        <KvRow
          k="Unused?"
          v={item.is_unused ? "Yes" : "No"}
          valueClass={item.is_unused ? "text-err" : "text-ok"}
        />
      </CardBox>
      <CardBox title="Details">
        {item.formatstring && <KvRow k="Format" v={item.formatstring} />}
        <KvRow k="Group ID" v={item.group_id} />
        <KvRow k="Item ID" v={item.item_id} />
        {item.web_url && (
          <KvRow
            k="Power BI"
            v={
              <a
                href={item.web_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-brand hover:underline"
              >
                Open <ExternalLink className="h-3 w-3" />
              </a>
            }
          />
        )}
      </CardBox>
    </div>
  );
}

/** colibri-style dbt metadata: database / schema / path / model type. */
function DbtCard({ item }: { item: Item }) {
  const modelType = detectModelType(item.item_name || item.table_name);
  const meta = MODEL_TYPE_META[modelType];
  const database = item.database_name || item.dataset_name;
  const schema = item.schema_name || item.bq_schema;
  return (
    <CardBox
      title="dbt"
      headerRight={
        <>
          {meta.badge && (
            <span
              className="rounded px-1.5 py-px text-[9px] font-bold tracking-wide text-white"
              style={{ background: meta.color }}
            >
              {meta.badge}
            </span>
          )}
          <span className="text-[11px] text-muted-foreground">{meta.label}</span>
        </>
      }
    >
      <KvRow k="Database" v={database} />
      <KvRow k="Schema" v={schema} />
      <KvRow k="Path" v={item.path} />
      {item.datatype && <KvRow k="Data type" v={item.datatype} />}
    </CardBox>
  );
}

function CardBox({
  title,
  headerRight,
  children,
}: {
  title: string;
  headerRight?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-line bg-panel2 p-3">
      <div className="mb-2.5 flex items-center gap-2">
        <h4 className="text-[10px] font-semibold uppercase tracking-[0.06em] text-faint">{title}</h4>
        {headerRight}
      </div>
      <dl className="space-y-1.5 text-[12.5px]">{children}</dl>
    </div>
  );
}

function KvRow({ k, v, valueClass }: { k: string; v?: React.ReactNode; valueClass?: string }) {
  const empty = v === null || v === undefined || v === "";
  return (
    <div className="flex justify-between gap-3">
      <dt className="shrink-0 text-muted-foreground">{k}</dt>
      <dd className={cn("min-w-0 break-words text-right font-medium", valueClass)}>{empty ? "—" : v}</dd>
    </div>
  );
}

function Description({ item }: { item: Item }) {
  if (!item.custom_description && !item.description) return null;
  return (
    <div className="space-y-3 rounded-lg border border-line bg-panel2 p-3.5">
      {item.custom_description && (
        <div>
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-faint">
            Annotation (curated)
          </div>
          <p className="whitespace-pre-wrap text-[12.5px] text-foreground/90">{item.custom_description}</p>
        </div>
      )}
      {item.description && (
        <div>
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-faint">
            Source description
          </div>
          <p className="whitespace-pre-wrap text-[12.5px] text-muted-foreground">{item.description}</p>
        </div>
      )}
    </div>
  );
}

function ConnectedReports({ item, instances }: { item: Item; instances: Item[] }) {
  const reports = instances.length > 1 ? aggregateReports(instances) : item.connected_reports_json ?? [];
  return (
    <div className="rounded-lg border border-line bg-panel2 p-3.5">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.06em] text-faint">
        Used in reports ({reports.length})
      </div>
      <div className="text-[12.5px]">
        {reports.length ? (
          reports.map((rp, i) => (
            <span key={(rp.id || rp.name || i) + ""}>
              {i > 0 && ", "}
              {rp.url ? (
                <a href={rp.url} target="_blank" rel="noreferrer" className="text-brand hover:underline">
                  {rp.name || rp.id}
                </a>
              ) : (
                <span>{rp.name || rp.id}</span>
              )}
            </span>
          ))
        ) : (
          <span className="text-faint">No downstream reports.</span>
        )}
      </div>
    </div>
  );
}

function InstancesTable({
  instances,
  rep,
  onSetPrimary,
  onShowDax,
}: {
  instances: Item[];
  rep: Item;
  onSetPrimary?: (itemId: string) => void;
  onShowDax: (dax: { name: string; expr: string }) => void;
}) {
  const sorted = sortInstances(instances);
  return (
    <div className="overflow-hidden rounded-lg border border-line">
      <div className="border-b border-line bg-panel2 px-3 py-2 text-[10px] font-semibold uppercase tracking-[0.06em] text-faint">
        Instances ({instances.length}) — ★ = current default (pinned primary if set, else non-external then
        workspace priority). Use “Set primary” to pin one.
      </div>
      <div className="max-h-[40vh] overflow-auto">
        <table className="w-full text-left text-[12px]">
          <thead className="sticky top-0 bg-card text-[10px] uppercase tracking-[0.06em] text-faint">
            <tr>
              <th className="px-3 py-2">Workspace</th>
              <th className="px-3 py-2">Dataset</th>
              <th className="px-3 py-2">Table</th>
              <th className="px-3 py-2">Kind</th>
              <th className="px-3 py-2">Reports</th>
              <th className="px-3 py-2">DAX</th>
              <th className="px-3 py-2">Primary</th>
              <th className="px-3 py-2">Link</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((x) => {
              const isRep = x.item_id === rep.item_id;
              const ext = isExternalMeasure(x);
              return (
                <tr key={x.item_id} className={cn("border-b border-line", isRep && "bg-brand/5")}>
                  <td className="px-3 py-2">
                    {isRep && (
                      <span className="font-bold text-brand" title="Current default">
                        ★{" "}
                      </span>
                    )}
                    {x.workspace_name || "—"}
                  </td>
                  <td className="px-3 py-2">{x.dataset_name || "—"}</td>
                  <td className="px-3 py-2">{x.table_name || "—"}</td>
                  <td className="px-3 py-2">
                    {ext ? <Badge variant="warning">external</Badge> : <span className="text-faint">internal</span>}
                  </td>
                  <td className="px-3 py-2">{(x.connected_reports_json || []).length}</td>
                  <td className="px-3 py-2">
                    {x.expression ? (
                      <button
                        type="button"
                        onClick={() => onShowDax({ name: x.item_name || "", expr: x.expression || "" })}
                        className="rounded-full border border-info/30 bg-info/10 px-2 py-0.5 font-mono text-[10px] font-bold text-info hover:bg-info/20"
                      >
                        f(x)
                      </button>
                    ) : (
                      <span className="text-faint">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {x.is_primary ? (
                      <Badge variant="warning">★ Pinned</Badge>
                    ) : onSetPrimary ? (
                      <button
                        type="button"
                        onClick={() => onSetPrimary(x.item_id)}
                        className="rounded-full border border-line-strong bg-panel px-2 py-0.5 text-[10px] font-bold text-muted-foreground transition-colors hover:bg-brand/15 hover:text-brand"
                      >
                        Set primary
                      </button>
                    ) : (
                      <span className="text-faint">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {x.web_url ? (
                      <a href={x.web_url} target="_blank" rel="noreferrer" className="text-brand hover:underline">
                        Open ↗
                      </a>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/**
 * Tabbed, syntax-highlighted code viewer that adapts to the item type:
 *   - measures / calc columns → DAX (raw `expression`)
 *   - dbt models / sources    → SQL with a raw ↔ compiled toggle, plus YAML
 * Only renders the tabs/toggles that the item actually has.
 */
function CodeArea({ item, isGroup }: { item: Item; isGroup: boolean }) {
  const raw = (item.expression ?? "").trim();
  const compiled = (item.compiled_expression ?? "").trim();
  const yaml = (item.properties_yaml ?? "").trim();
  const isDbt = DBT_TYPES.has(String(item.item_type));
  const hasSql = isDbt && !!(raw || compiled);
  const hasBothSql = isDbt && !!raw && !!compiled && raw !== compiled;
  const hasDax = !isDbt && !!raw;
  const hasYaml = !!yaml;

  const tabs: ("dax" | "sql" | "yaml")[] = [];
  if (hasDax) tabs.push("dax");
  if (hasSql) tabs.push("sql");
  if (hasYaml) tabs.push("yaml");

  const [tab, setTab] = useState<"dax" | "sql" | "yaml">(tabs[0] ?? "dax");
  const [sqlMode, setSqlMode] = useState<"compiled" | "raw">("compiled");
  const [copied, setCopied] = useState(false);

  if (tabs.length === 0) return null;
  const active = tabs.includes(tab) ? tab : tabs[0];

  const sqlText = sqlMode === "compiled" ? compiled || raw : raw || compiled;
  const text = active === "dax" ? raw : active === "yaml" ? yaml : sqlText;
  const lang: CodeLang = active === "yaml" ? "yaml" : "sql";

  const exprLabel = item.item_type === "PB_MEASURE" ? "DAX" : "Expression";
  const tabLabel: Record<"dax" | "sql" | "yaml", string> = {
    dax: isGroup ? `${exprLabel} · default instance` : exprLabel,
    sql: "SQL",
    yaml: "YAML",
  };

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked — no-op */
    }
  }

  return (
    <div className="overflow-hidden rounded-lg border border-line">
      <div className="flex items-center justify-between gap-2 border-b border-line bg-panel2 px-2 py-1.5">
        <div className="flex gap-0.5">
          {tabs.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className={cn(
                "rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors",
                active === t ? "bg-card text-foreground shadow-sm" : "text-faint hover:text-foreground",
              )}
            >
              {tabLabel[t]}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1.5">
          {active === "sql" && hasBothSql && (
            <div className="inline-flex items-center rounded-md border border-line bg-card p-0.5">
              {(["compiled", "raw"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setSqlMode(m)}
                  className={cn(
                    "rounded px-2 py-0.5 text-[10.5px] font-medium capitalize transition-colors",
                    sqlMode === m ? "bg-panel2 text-foreground" : "text-faint hover:text-foreground",
                  )}
                >
                  {m}
                </button>
              ))}
            </div>
          )}
          <button
            type="button"
            onClick={copy}
            title="Copy"
            className="grid h-6 w-6 place-items-center rounded text-faint hover:bg-panel hover:text-foreground"
          >
            {copied ? <Check className="h-3.5 w-3.5 text-brand" /> : <Copy className="h-3.5 w-3.5" />}
          </button>
        </div>
      </div>
      <div className="max-h-[260px] overflow-auto bg-panel2/40 px-3 py-2">
        <CodeBlock code={text} language={lang} />
      </div>
    </div>
  );
}
