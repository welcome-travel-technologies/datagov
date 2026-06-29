"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { X, Search, Layers, Sigma, BarChart3, Database, Eye, Box, Code2, Braces, Copy, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { CodeBlock } from "@/components/ui/code-block";
import { GROUP_LABELS } from "@/lib/lineage/graph-utils";
import { MODEL_TYPE_META, showModelTypeBadge } from "@/lib/lineage/colibri";
import { cardAccent, cardLayer, type Lens } from "@/lib/lineage/lens";
import type { ModelCard } from "@/lib/lineage/column-model";
import { api, type Item } from "@/lib/api";

/** Pull the "TYPE::hash" hash out of a composite node id (mirrors NodeDetailPanel). */
function idHashOf(nodeId: string): string {
  const i = nodeId.indexOf("::");
  return i >= 0 ? nodeId.slice(i + 2) : nodeId;
}

/** Same layer→icon mapping the canvas card header uses, so the panel matches. */
function KindIcon({ card }: { card: ModelCard }) {
  const cls = "h-4 w-4 shrink-0";
  const layer = cardLayer(card);
  if (layer === "measures") return <Sigma className={cls} />;
  if (layer === "reports") return <BarChart3 className={cls} />;
  if (layer === "source" || layer === "seed" || layer === "powerbi") return <Database className={cls} />;
  if (layer === "staging") return <Eye className={cls} />;
  return <Layers className={cls} />;
}

/** Build a colibri-style `"DB"."SCHEMA"."table"` relation from whatever the item carries. */
function relationOf(item: Item | null, fallbackTable: string): string | null {
  if (!item) return null;
  const db = item.database_name || item.dataset_name;
  const schema = item.schema_name || item.bq_schema;
  const table = item.table_name || item.item_name || fallbackTable;
  const parts = [db, schema, table].filter(Boolean) as string[];
  return parts.length ? parts.map((p) => `"${p}"`).join(".") : null;
}

/**
 * Colibri-style right detail panel for the selected model card. Columns / type /
 * tags come from the already-built graph card (instant); description, database
 * relation and file path are hydrated from the items API.
 */
export function ModelDetailSidebar({
  card,
  width,
  lens,
  selectedColId,
  onColumnClick,
  onClose,
}: {
  card: ModelCard;
  /** Panel width (px), controlled by the parent's resize handle. */
  width: number;
  lens: Lens;
  selectedColId: string | null;
  onColumnClick: (colId: string) => void;
  onClose: () => void;
}) {
  const [item, setItem] = useState<Item | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState("");
  const [tab, setTab] = useState<"info" | "code" | "yaml">("info");
  const [sqlMode, setSqlMode] = useState<"compiled" | "raw">("compiled");
  const [copied, setCopied] = useState("");
  // The selected column's item — its `expression` is a measure's DAX / a
  // calculated column's formula, surfaced in the Code tab.
  const [colItem, setColItem] = useState<Item | null>(null);

  // Hydrate the richer metadata for this card. Keyed on the card id so switching
  // models refetches but re-renders of the same selection do not.
  useEffect(() => {
    let cancelled = false;
    setItem(null);
    setLoading(true);
    setFilter("");
    (async () => {
      try {
        const it = await api.items.get(idHashOf(card.id));
        if (!cancelled && it && it.item_id) {
          setItem(it);
          return;
        }
        throw new Error("no item");
      } catch {
        try {
          const data = await api.items.byName(card.label);
          if (!cancelled && data.results && data.results.length) setItem(data.results[0]);
        } catch {
          /* metadata stays empty — columns still render from the card */
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [card.id, card.label]);

  // Fetch the selected column's item (for its expression / DAX). Cheap single
  // GET; only matters when a column is pinned in this card.
  useEffect(() => {
    if (!selectedColId) {
      setColItem(null);
      return;
    }
    let cancelled = false;
    setColItem(null);
    (async () => {
      try {
        const it = await api.items.get(idHashOf(selectedColId));
        if (!cancelled && it && it.item_id) setColItem(it);
      } catch {
        /* no item — Code tab falls back to the model SQL */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedColId]);

  const accent = cardAccent(card);
  const nodeTypeLabel = GROUP_LABELS[card.group] ?? card.group;
  const modelMeta = MODEL_TYPE_META[card.modelType];
  const relation = relationOf(item, card.label);
  const description = item?.custom_description || item?.description;
  const tags = card.tags.length ? card.tags : ((item?.tags as string[] | undefined) ?? []);

  const cols = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return q ? card.columns.filter((c) => c.label.toLowerCase().includes(q)) : card.columns;
  }, [card.columns, filter]);

  // Code tab: raw lives in `expression`, compiled in `compiled_expression`.
  const rawSql = (item?.expression ?? "").trim();
  const compiledSql = (item?.compiled_expression ?? "").trim();
  const hasSql = !!(rawSql || compiledSql);
  const hasBothSql = !!rawSql && !!compiledSql && rawSql !== compiledSql;
  const shownSql = sqlMode === "compiled" ? compiledSql || rawSql : rawSql || compiledSql;

  // A pinned column's own expression (a measure's DAX / calculated-column formula)
  // takes precedence in the Code tab over the model-level SQL.
  const selectedCol = selectedColId ? card.columns.find((c) => c.id === selectedColId) ?? null : null;
  const colExpr = (colItem?.expression ?? "").trim();
  const showColExpr = !!selectedCol && !!colExpr;
  const codeText = showColExpr ? colExpr : shownSql;
  const codeLabel = showColExpr ? `${selectedCol!.isMeasure ? "DAX" : "Expression"} · ${selectedCol!.label}` : "";
  const hasCode = showColExpr || hasSql;
  const isMeasuresCard = card.cardKind === "measures";

  // YAML tab: authored schema.yml properties.
  const yamlText = (item?.properties_yaml ?? "").trim();
  const hasYaml = !!yamlText;

  async function copy(key: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied(""), 2000);
    } catch {
      /* clipboard blocked — no-op */
    }
  }

  return (
    <aside
      className="flex h-full min-h-0 shrink-0 flex-col overflow-hidden rounded-lg border border-line bg-card"
      style={{ width }}
    >
      {/* header */}
      <div className="flex items-start gap-2 border-b border-line px-3 py-2.5">
        <span className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded" style={{ color: accent }}>
          <KindIcon card={card} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-semibold leading-tight" title={card.label}>
            {card.label}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5">
            <span className="text-[11px] text-faint">{nodeTypeLabel}</span>
            {showModelTypeBadge(card.group, card.modelType) && modelMeta.badge && (
              <span
                className="rounded px-1 py-px text-[8.5px] font-bold leading-none tracking-wide text-white"
                style={{ background: modelMeta.color }}
              >
                {modelMeta.badge}
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          title="Close details"
          className="grid h-6 w-6 shrink-0 place-items-center rounded text-faint hover:bg-panel2 hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* body — Info / Code tabs (mirrors dbt-colibri's detail panel) */}
      <Tabs
        value={tab}
        onValueChange={(v) => setTab(v as "info" | "code" | "yaml")}
        className="flex min-h-0 flex-1 flex-col"
      >
        <TabsList className="shrink-0 gap-3 px-3">
          <TabsTrigger value="info" className="flex items-center gap-1.5 px-1">
            <Box className="h-3.5 w-3.5" /> Info
          </TabsTrigger>
          <TabsTrigger value="code" className="flex items-center gap-1.5 px-1">
            <Code2 className="h-3.5 w-3.5" /> Code
          </TabsTrigger>
          <TabsTrigger value="yaml" className="flex items-center gap-1.5 px-1">
            <Braces className="h-3.5 w-3.5" /> YAML
          </TabsTrigger>
        </TabsList>

        {/* fill area — each panel is absolutely positioned so it always takes
            the full height regardless of how much content it holds */}
        <div className="relative min-h-0 flex-1">
        <TabsContent value="info" className="absolute inset-0 mt-0 overflow-y-auto">
        <Section title="Description">
          {description ? (
            <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-foreground/80">{description}</p>
          ) : (
            <p className="text-[12.5px] italic text-faint">{loading ? "Loading…" : "No description."}</p>
          )}
        </Section>

        {relation && (
          <Section title="Database relation">
            <code className="block break-all rounded-md border border-line bg-panel2 px-2.5 py-2 font-mono text-[11.5px] text-foreground/80">
              {relation}
            </code>
          </Section>
        )}

        <Section title="Details">
          <dl className="space-y-1.5 text-[12.5px]">
            <Row k="Node type" v={nodeTypeLabel} />
            <Row k="Model type" v={modelMeta.label} />
            {item?.path && <Row k="Path" v={item.path} mono />}
            {item?.datatype && <Row k="Data type" v={item.datatype} />}
          </dl>
          {tags.length > 0 && (
            <div className="mt-2.5 flex flex-wrap gap-1">
              {tags.map((t) => (
                <Badge key={t} variant="default">
                  {t}
                </Badge>
              ))}
            </div>
          )}
        </Section>

        <Section title={`Columns (${card.columns.length})`}>
          {card.columns.length > 6 && (
            <div className="relative mb-2">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Find a column…"
                className="h-7 w-full rounded-md border border-line bg-panel2 pl-7 pr-2 text-[12px] outline-none placeholder:text-faint focus:border-brand"
              />
            </div>
          )}
          <ul className="space-y-0.5">
            {cols.map((col) => {
              const badge = lens.columnBadge(col);
              const active = selectedColId === col.id;
              return (
                <li key={col.id}>
                  <button
                    type="button"
                    onClick={() => onColumnClick(col.id)}
                    title={`${col.label} — click to trace lineage`}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left font-mono text-[11.5px] transition-colors",
                      active ? "bg-brand/12 text-brand" : "text-foreground/80 hover:bg-panel2",
                    )}
                  >
                    <span className="w-4 shrink-0 text-center text-faint">{col.glyph}</span>
                    <span className="min-w-0 flex-1 truncate">{col.label}</span>
                    {col.datatype && <span className="shrink-0 text-[10px] text-faint">{col.datatype}</span>}
                    {badge && (
                      <span
                        className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-[8.5px] font-bold leading-none"
                        style={{ color: badge.color, background: badge.color + "22" }}
                        title={badge.text}
                      >
                        {badge.text}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
            {cols.length === 0 && (
              <li className="px-2 py-1 text-[12px] italic text-faint">
                {card.columns.length === 0 ? "No columns loaded for this model." : "No columns match."}
              </li>
            )}
          </ul>
        </Section>
        </TabsContent>

        <TabsContent value="code" className="absolute inset-0 mt-0 flex flex-col">
          {!item && loading ? (
            <p className="px-3 py-4 text-[12.5px] italic text-faint">Loading…</p>
          ) : !hasCode ? (
            <p className="px-3 py-4 text-[12.5px] italic text-faint">
              {isMeasuresCard ? "Select a measure to view its DAX." : "No code for this model."}
            </p>
          ) : (
            <>
              <div className="flex shrink-0 items-center justify-between gap-2 px-3 py-2">
                <span className="min-w-0 truncate text-[11px] font-medium text-faint" title={codeLabel}>
                  {codeLabel}
                </span>
                <div className="flex shrink-0 items-center gap-1.5">
                  {!showColExpr && hasBothSql && (
                    <div className="inline-flex items-center rounded-md border border-line bg-panel2 p-0.5">
                      {(["compiled", "raw"] as const).map((m) => (
                        <button
                          key={m}
                          type="button"
                          onClick={() => setSqlMode(m)}
                          className={cn(
                            "rounded px-2.5 py-1 text-[11px] font-medium capitalize transition-colors",
                            sqlMode === m
                              ? "bg-card text-foreground shadow-sm"
                              : "text-faint hover:text-foreground",
                          )}
                        >
                          {m}
                        </button>
                      ))}
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => copy("sql", codeText)}
                    title="Copy"
                    className="grid h-7 w-7 shrink-0 place-items-center rounded text-faint hover:bg-panel2 hover:text-foreground"
                  >
                    {copied === "sql" ? <Check className="h-3.5 w-3.5 text-brand" /> : <Copy className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </div>
              <div className="min-h-0 flex-1 overflow-auto border-t border-line bg-panel2/40 px-3 py-2">
                <CodeBlock code={codeText} language="sql" />
              </div>
            </>
          )}
        </TabsContent>

        <TabsContent value="yaml" className="absolute inset-0 mt-0 flex flex-col">
          {!item && loading ? (
            <p className="px-3 py-4 text-[12.5px] italic text-faint">Loading…</p>
          ) : !hasYaml ? (
            <p className="px-3 py-4 text-[12.5px] italic text-faint">No YAML properties for this model.</p>
          ) : (
            <>
              <div className="flex shrink-0 items-center justify-end px-3 py-2">
                <button
                  type="button"
                  onClick={() => copy("yaml", yamlText)}
                  title="Copy YAML"
                  className="grid h-7 w-7 shrink-0 place-items-center rounded text-faint hover:bg-panel2 hover:text-foreground"
                >
                  {copied === "yaml" ? <Check className="h-3.5 w-3.5 text-brand" /> : <Copy className="h-3.5 w-3.5" />}
                </button>
              </div>
              <div className="min-h-0 flex-1 overflow-auto border-t border-line bg-panel2/40 px-3 py-2">
                <CodeBlock code={yamlText} language="yaml" />
              </div>
            </>
          )}
        </TabsContent>
        </div>
      </Tabs>
    </aside>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="border-b border-line px-3 py-3">
      <h4 className="mb-2 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-faint">{title}</h4>
      {children}
    </div>
  );
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="shrink-0 text-muted-foreground">{k}</dt>
      <dd className={cn("min-w-0 truncate text-right font-medium", mono && "font-mono text-[11px]")} title={v}>
        {v}
      </dd>
    </div>
  );
}
