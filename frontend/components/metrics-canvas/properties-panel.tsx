"use client";

import { Trash2, Group, Ungroup } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { LoadingState } from "@/components/ui/misc";
import { ItemDetail, badgeVariant, useItemDetail } from "@/components/items/item-detail";
import { typeMeta } from "@/lib/metrics-canvas/catalog";
import type { RfEdge, RfNode } from "@/lib/metrics-canvas/serialize";
import type { CanvasEdgeData, CanvasGroup, CanvasMeta, CanvasNodeData, CatalogRef } from "@/lib/metrics-canvas/types";
import { DEFAULT_FONT_SCALE, FONT_SCALE_MAX, FONT_SCALE_MIN } from "@/lib/metrics-canvas/types";

const ARROW = { type: "arrowclosed", width: 16, height: 16 };

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex items-center justify-between gap-2 py-1">
      <span className="shrink-0 text-[11.5px] text-muted-foreground">{label}</span>
      <div className="flex min-w-0 flex-1 justify-end">{children}</div>
    </label>
  );
}

function TextField({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <input
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="h-7 w-full min-w-0 rounded-md border border-input bg-panel px-2 text-[12px] outline-none focus:ring-1 focus:ring-ring"
    />
  );
}

function ColorField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <input
      type="color"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="h-7 w-10 cursor-pointer rounded border border-input bg-panel"
    />
  );
}

/** Compact swatch that opens the native color picker — sized to sit inline in a
 *  list row (the group list), unlike the larger {@link ColorField}. */
function SwatchColorField({ value, onChange, title }: { value: string; onChange: (v: string) => void; title?: string }) {
  return (
    <label className="relative h-4 w-4 shrink-0 cursor-pointer" title={title}>
      <span className="block h-full w-full rounded-sm border border-line-strong" style={{ background: value }} />
      <input
        type="color"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="absolute inset-0 cursor-pointer opacity-0"
      />
    </label>
  );
}

function Toggle({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "rounded-md border px-2 py-1 text-[11.5px] font-medium transition-colors",
        on ? "border-brand bg-brand/10 text-brand" : "border-line-strong bg-panel text-muted-foreground hover:bg-panel2",
      )}
    >
      {label}
    </button>
  );
}

/** Text-size slider with a click-to-reset percentage readout. Used both for the
 *  map-wide scale and for a single element's override (relative to that scale). */
function FontSizeField({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <div className="flex items-center gap-2">
      <input
        type="range"
        min={FONT_SCALE_MIN}
        max={FONT_SCALE_MAX}
        step={0.1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-24"
      />
      <button
        type="button"
        onClick={() => onChange(DEFAULT_FONT_SCALE)}
        title="Reset to 100%"
        className="w-10 shrink-0 rounded text-right text-[11px] tabular-nums text-muted-foreground hover:text-foreground"
      >
        {Math.round(value * 100)}%
      </button>
    </div>
  );
}

export interface PropertiesPanelProps {
  node: RfNode | null;
  edge: RfEdge | null;
  /** The selected group, when a group (not a node/edge) is selected. */
  selectedGroup: CanvasGroup | null;
  meta: CanvasMeta;
  groups: CanvasGroup[];
  selectionCount: number;
  onPatchNodeData: (id: string, patch: Partial<CanvasNodeData>) => void;
  onPatchEdge: (id: string, patch: Partial<RfEdge>) => void;
  onPatchMeta: (patch: Partial<CanvasMeta>) => void;
  onDeleteSelection: () => void;
  onGroupSelection: () => void;
  onUngroup: (groupId: string) => void;
  onRenameGroup: (groupId: string, name: string) => void;
  onSetGroupColor: (groupId: string, color: string) => void;
}

export function PropertiesPanel(props: PropertiesPanelProps) {
  const { node, edge, selectedGroup } = props;

  // A selected group takes precedence: clicking a group's label clears the
  // node/edge selection, so the group panel is what the user expects to see.
  if (selectedGroup) return <GroupProps {...props} group={selectedGroup} />;
  if (node) return <NodeProps {...props} node={node} />;
  if (edge) return <EdgeProps {...props} edge={edge} />;
  return <CanvasProps {...props} />;
}

/** Settings for the selected group: rename, recolor, or ungroup all members
 *  (the nodes themselves are kept — only the grouping is removed). */
function GroupProps({ group, onRenameGroup, onSetGroupColor, onUngroup }: PropertiesPanelProps & { group: CanvasGroup }) {
  return (
    <div className="space-y-2 p-3">
      <Header icon="▦" title="Group" />
      <Row label="Name">
        <TextField value={group.name} onChange={(v) => onRenameGroup(group.id, v)} />
      </Row>
      <Row label="Color">
        <ColorField value={group.color} onChange={(v) => onSetGroupColor(group.id, v)} />
      </Row>
      <p className="pt-1 text-[11.5px] text-faint">
        {group.nodeIds.length} item{group.nodeIds.length === 1 ? "" : "s"} in this group.
      </p>
      <div className="pt-1">
        <Button size="sm" variant="outline" onClick={() => onUngroup(group.id)}>
          <Ungroup className="h-3.5 w-3.5" /> Ungroup all items
        </Button>
      </div>
    </div>
  );
}

function NodeProps({ node, onPatchNodeData, onDeleteSelection, selectionCount, onGroupSelection }: PropertiesPanelProps & { node: RfNode }) {
  const d = node.data as CanvasNodeData;
  const meta = typeMeta(d.elementType);
  const isShape = node.type === "shape";
  const isFillable = isShape || d.elementType === "sticky";
  const patch = (p: Partial<CanvasNodeData>) => onPatchNodeData(node.id, p);

  return (
    <div className="space-y-2 p-3">
      {selectionCount > 1 && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-brand/30 bg-brand/5 px-2.5 py-1.5">
          <span className="text-[11.5px] font-medium text-brand">{selectionCount} nodes selected</span>
          <Button size="sm" variant="outline" onClick={onGroupSelection}>
            <Group className="h-3.5 w-3.5" /> Group
          </Button>
        </div>
      )}
      <Header icon={meta.icon} title={meta.label} />
      <Row label="Label">
        <TextField value={d.label} onChange={(v) => patch({ label: v })} />
      </Row>
      {node.type === "element" && (
        <Row label="Subtitle">
          <TextField value={d.sub ?? ""} onChange={(v) => patch({ sub: v })} placeholder="—" />
        </Row>
      )}
      <Row label="Border">
        <ColorField value={d.borderColor || meta.color} onChange={(v) => patch({ borderColor: v })} />
      </Row>
      {isFillable && (
        <>
          <Row label="Fill">
            <ColorField value={d.fillColor || meta.color} onChange={(v) => patch({ fillColor: v })} />
          </Row>
          {isShape && (
            <Row label="Fill opacity">
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={d.fillAlpha ?? 0.12}
                onChange={(e) => patch({ fillAlpha: Number(e.target.value) })}
                className="w-28"
              />
            </Row>
          )}
        </>
      )}
      <Row label="Font size">
        <FontSizeField value={d.fontScale ?? DEFAULT_FONT_SCALE} onChange={(v) => patch({ fontScale: v })} />
      </Row>
      {d.meta && <CatalogNodeDetails meta={d.meta} label={d.label} />}
      <div className="flex flex-wrap gap-1.5 pt-1">
        <Button size="sm" variant="ghost" onClick={onDeleteSelection} className="text-err hover:bg-err/10 hover:text-err">
          <Trash2 className="h-3.5 w-3.5" /> Delete
        </Button>
      </div>
    </div>
  );
}

/**
 * Rich catalog details for a node dropped from the catalog palette — the same
 * characteristics surfaced in the data-lineage detail view (context, usage,
 * description, expression, connected reports, Power BI link). Fetched lazily
 * from the node's captured `itemId`, with a name-search fallback.
 */
function CatalogNodeDetails({ meta, label }: { meta: CatalogRef; label: string }) {
  const { item, loading, notFound } = useItemDetail(meta.itemId || null, label || meta.itemName);

  return (
    <div className="mt-2 border-t border-line pt-3">
      <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">
        From catalog
        {item?.item_type && <Badge variant={badgeVariant(item.item_type)}>{item.item_type}</Badge>}
      </div>
      {loading && <LoadingState label="Fetching details…" />}
      {!loading && notFound && (
        <p className="py-3 text-center text-[12px] text-muted-foreground">
          No detailed characteristics found for this item.
        </p>
      )}
      {!loading && item && <ItemDetail item={item} dense />}
    </div>
  );
}

function EdgeProps({ edge, onPatchEdge, onDeleteSelection }: PropertiesPanelProps & { edge: RfEdge }) {
  const data = (edge.data ?? {}) as CanvasEdgeData;
  const color = data.color || "#64748b";
  const hasEnd = !!edge.markerEnd;
  const hasStart = !!edge.markerStart;
  const dashed = !!data.dashed;

  function patch(p: { arrowStart?: boolean; arrowEnd?: boolean; dashed?: boolean; color?: string; label?: string }) {
    const nextColor = p.color ?? color;
    const nextDashed = p.dashed ?? dashed;
    const nextEnd = p.arrowEnd ?? hasEnd;
    const nextStart = p.arrowStart ?? hasStart;
    onPatchEdge(edge.id, {
      label: p.label ?? (typeof edge.label === "string" ? edge.label : ""),
      data: { ...data, color: nextColor, dashed: nextDashed, arrowStart: nextStart, arrowEnd: nextEnd },
      markerEnd: nextEnd ? ({ ...ARROW, color: nextColor } as never) : undefined,
      markerStart: nextStart ? ({ ...ARROW, color: nextColor } as never) : undefined,
      style: { stroke: nextColor, strokeWidth: 2, ...(nextDashed ? { strokeDasharray: "6 4" } : {}) },
    });
  }

  return (
    <div className="space-y-2 p-3">
      <Header icon="⤳" title="Arrow" />
      <Row label="Label">
        <TextField value={typeof edge.label === "string" ? edge.label : ""} onChange={(v) => patch({ label: v })} placeholder="—" />
      </Row>
      <Row label="Color">
        <ColorField value={color} onChange={(v) => patch({ color: v })} />
      </Row>
      <Row label="Arrowheads">
        <div className="flex gap-1.5">
          <Toggle on={hasStart} label="Start" onClick={() => patch({ arrowStart: !hasStart })} />
          <Toggle on={hasEnd} label="End" onClick={() => patch({ arrowEnd: !hasEnd })} />
        </div>
      </Row>
      <Row label="Style">
        <Toggle on={dashed} label="Dashed" onClick={() => patch({ dashed: !dashed })} />
      </Row>
      <div className="pt-1">
        <Button size="sm" variant="ghost" onClick={onDeleteSelection} className="text-err hover:bg-err/10 hover:text-err">
          <Trash2 className="h-3.5 w-3.5" /> Delete arrow
        </Button>
      </div>
    </div>
  );
}

function CanvasProps({ meta, onPatchMeta, groups, onUngroup, onRenameGroup, onSetGroupColor }: PropertiesPanelProps) {
  const fontScale = meta.fontScale ?? DEFAULT_FONT_SCALE;
  return (
    <div className="space-y-2 p-3">
      <Header icon="◫" title="Map" />
      <Row label="Name">
        <TextField value={meta.name} onChange={(v) => onPatchMeta({ name: v })} />
      </Row>
      <div className="py-1">
        <span className="mb-1 block text-[11.5px] text-muted-foreground">Description</span>
        <textarea
          value={meta.description ?? ""}
          onChange={(e) => onPatchMeta({ description: e.target.value })}
          placeholder="What is this map about?"
          rows={3}
          className="w-full resize-none rounded-md border border-input bg-panel px-2 py-1.5 text-[12px] outline-none focus:ring-1 focus:ring-ring"
        />
      </div>

      <Row label="Font size">
        <FontSizeField value={fontScale} onChange={(v) => onPatchMeta({ fontScale: v })} />
      </Row>

      <div className="pt-1">
        <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">
          Groups ({groups.length})
        </div>
        {groups.length === 0 ? (
          <p className="text-[11.5px] text-faint">Select 2+ nodes and click Group to box them together.</p>
        ) : (
          <div className="space-y-1">
            {groups.map((g) => (
              <div key={g.id} className="flex items-center gap-1.5">
                <SwatchColorField value={g.color} onChange={(c) => onSetGroupColor(g.id, c)} title="Change group color" />
                <input
                  value={g.name}
                  onChange={(e) => onRenameGroup(g.id, e.target.value)}
                  className="h-7 min-w-0 flex-1 rounded-md border border-input bg-panel px-2 text-[12px] outline-none focus:ring-1 focus:ring-ring"
                />
                <button
                  onClick={() => onUngroup(g.id)}
                  title="Ungroup"
                  className="grid h-7 w-7 shrink-0 place-items-center rounded text-faint hover:bg-panel2 hover:text-foreground"
                >
                  <Ungroup className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Header({ icon, title }: { icon: string; title: string }) {
  return (
    <div className="flex items-center gap-2 border-b border-line pb-2">
      <span className="text-[15px] leading-none">{icon}</span>
      <span className="text-[12.5px] font-semibold">{title}</span>
    </div>
  );
}
