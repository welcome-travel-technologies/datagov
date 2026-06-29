"use client";

import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { ChevronDown, ChevronRight, Plus, Layers, Sigma, BarChart3, Database, Eye } from "lucide-react";
import { cn } from "@/lib/utils";
import { GROUP_LABELS } from "@/lib/lineage/graph-utils";
import { MODEL_TYPE_META, showModelTypeBadge } from "@/lib/lineage/colibri";
import { cardAccent, cardLayer } from "@/lib/lineage/lens";
import { CARD_WIDTH, CARD_HEADER_H, ROW_H } from "@/lib/lineage/layout";
import { type ModelCard, SELF_EDGE_TARGET_SUFFIX } from "@/lib/lineage/column-model";
import { useCanvas } from "@/components/lineage/canvas/context";

export interface CardNodeData {
  card: ModelCard;
  collapsed?: boolean;
  /** When a card is collapsed but a column trace is pinned, the subset of column
   *  ids kept visible (the connected path). Absent → collapse to a header. */
  shownColIds?: string[];
}

/** A synthetic card (measures bucket / by-type group) has no real graph node, so
 *  it can't be expanded upstream/downstream. */
function isSynthetic(id: string): boolean {
  return id.startsWith("__");
}

function KindIcon({ card }: { card: ModelCard }) {
  const cls = "h-3.5 w-3.5 shrink-0";
  const layer = cardLayer(card);
  if (layer === "measures") return <Sigma className={cls} />;
  if (layer === "reports") return <BarChart3 className={cls} />;
  if (layer === "source" || layer === "seed" || layer === "powerbi") return <Database className={cls} />;
  if (layer === "staging") return <Eye className={cls} />;
  return <Layers className={cls} />;
}

function ModelTypeBadge({ type }: { type: keyof typeof MODEL_TYPE_META }) {
  const meta = MODEL_TYPE_META[type];
  if (!meta.badge) return null;
  return (
    <span
      className="shrink-0 rounded px-1 py-px text-[8.5px] font-bold leading-none tracking-wide text-white"
      style={{ background: meta.color }}
      title={`${meta.label} model`}
    >
      {meta.badge}
    </span>
  );
}

export const CardNode = memo(function CardNode({ data }: NodeProps) {
  const { card, collapsed, shownColIds } = data as unknown as CardNodeData;
  const {
    highlight,
    lens,
    onColumnClick,
    onColumnHover,
    onColumnContext,
    onCardHeaderClick,
    onToggleCollapse,
    onExpandUpstream,
    onExpandDownstream,
  } = useCanvas();

  const accent = cardAccent(card);
  const cardDim = highlight.active && !highlight.cards.has(card.id);
  const isReport = card.cardKind === "report";
  const expandable = !isSynthetic(card.id) && !isReport;

  // Focused collapse: a collapsed card that keeps just its connected columns
  // visible (driven by a pinned column trace) instead of folding to a header.
  const shownSet = shownColIds && shownColIds.length > 0 ? new Set(shownColIds) : null;
  const focusedCollapse = !!collapsed && !!shownSet;
  const showColumns = !isReport && (!collapsed || focusedCollapse);
  const renderedColumns = shownSet ? card.columns.filter((c) => shownSet.has(c.id)) : card.columns;

  return (
    <div
      className={cn(
        "rounded-lg border shadow-card transition-opacity",
        card.isCenter && "ring-2 ring-brand/40",
        cardDim && "opacity-30",
      )}
      style={{ width: CARD_WIDTH, background: accent + "0e", borderColor: accent + "55" }}
    >
      {/* header */}
      <div
        className="flex items-center gap-1.5 rounded-t-lg border-b border-line px-2"
        style={{ height: CARD_HEADER_H }}
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onToggleCollapse(card.id);
          }}
          className="grid h-5 w-5 shrink-0 place-items-center rounded text-faint hover:bg-panel2 hover:text-foreground"
          title={collapsed ? "Expand columns" : "Collapse columns"}
        >
          {collapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onCardHeaderClick(card.id);
          }}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          title={`${card.label} — ${GROUP_LABELS[card.group] ?? card.group}`}
        >
          <span className="grid h-5 w-5 shrink-0 place-items-center rounded" style={{ color: accent }}>
            <KindIcon card={card} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[12.5px] font-semibold leading-tight">{card.label}</span>
            <span className="block text-[10.5px] text-faint">
              {isReport
                ? (GROUP_LABELS[card.group] ?? card.group)
                : card.cardKind === "measures"
                  ? "measure"
                  : `${card.columns.length} ${card.columns.length === 1 ? "column" : "columns"}`}
            </span>
          </span>
          {showModelTypeBadge(card.group, card.modelType) && <ModelTypeBadge type={card.modelType} />}
        </button>
      </div>

      {isReport ? (
        // header-only consumer card: usage edges attach to the card itself
        <>
          <Handle type="target" position={Position.Left} style={HANDLE_HIDDEN} isConnectable={false} />
          <Handle type="source" position={Position.Right} style={HANDLE_HIDDEN} isConnectable={false} />
        </>
      ) : (
        <>
          {!showColumns ? (
            // header-only collapse: column rows are hidden, so edges re-attach to
            // the card-level handles. The footer below stays available regardless.
            <>
              <Handle type="target" position={Position.Left} style={HANDLE_HIDDEN} isConnectable={false} />
              <Handle type="source" position={Position.Right} style={HANDLE_HIDDEN} isConnectable={false} />
            </>
          ) : (
            <div className="relative">
              {renderedColumns.map((col) => {
                // The handle lives inside this row (its `position: relative` offset
                // parent), so it must be centred within the row — NOT offset by the
                // row index, which would drift every successive handle downward.
                const top = ROW_H / 2;
                const rowActive = !highlight.active || highlight.cols.has(col.id);
                const isSelected = highlight.selectedCol === col.id;
                const badge = lens.columnBadge(col);
                return (
                  <div
                    key={col.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      onColumnClick(col.id);
                    }}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      onColumnContext(col.id, e.clientX, e.clientY);
                    }}
                    onMouseEnter={() => onColumnHover(col.id)}
                    onMouseLeave={() => onColumnHover(null)}
                    className={cn(
                      "relative flex cursor-pointer items-center gap-2 px-2.5 font-mono text-[11px] transition-opacity",
                      !rowActive && "opacity-20",
                      isSelected ? "bg-brand/12 text-brand" : "text-foreground/80 hover:bg-panel2",
                    )}
                    style={{ height: ROW_H }}
                    title={col.label}
                  >
                    <Handle
                      type="target"
                      position={Position.Left}
                      id={col.id}
                      style={{ top, ...HANDLE_HIDDEN }}
                      isConnectable={false}
                    />
                    <span className="w-4 shrink-0 text-center text-faint">{col.glyph}</span>
                    <span className="min-w-0 flex-1 truncate">{col.label}</span>
                    {badge && (
                      <span
                        className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-[8.5px] font-bold leading-none"
                        style={{ color: badge.color, background: badge.color + "22" }}
                        title={badge.text}
                      >
                        {badge.text}
                      </span>
                    )}
                    <Handle
                      type="source"
                      position={Position.Right}
                      id={col.id}
                      style={{ top, ...HANDLE_HIDDEN }}
                      isConnectable={false}
                    />
                    {/* Right-side target handle: intra-card (self-dependent) edges
                        land here so they loop on the right instead of wrapping
                        around the card to the left-side target handle. */}
                    <Handle
                      type="target"
                      position={Position.Right}
                      id={col.id + SELF_EDGE_TARGET_SUFFIX}
                      style={{ top, ...HANDLE_HIDDEN }}
                      isConnectable={false}
                    />
                  </div>
                );
              })}
            </div>
          )}

          {/* footer: load upstream (left) / downstream (right). Always shown for
              expandable cards — including collapsed ones — so the graph can be
              grown straight from a collapsed card without first expanding its
              columns. The top divider is dropped in header-only collapse, where
              the header's own bottom border already separates the footer. */}
          {expandable && (
            <div
              className={cn(
                "flex items-center justify-between px-2 py-1",
                showColumns && "border-t border-line",
              )}
            >
              <ExpandButton title="Load upstream models" onClick={() => onExpandUpstream(card.id)} />
              <ExpandButton title="Load downstream models" onClick={() => onExpandDownstream(card.id)} />
            </div>
          )}
        </>
      )}
    </div>
  );
});

const HANDLE_HIDDEN = { background: "transparent", border: "none" } as const;

function ExpandButton({ title, onClick }: { title: string; onClick: () => void }) {
  return (
    <button
      type="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className="grid h-6 w-6 place-items-center rounded-full border border-line text-faint hover:border-brand hover:text-brand"
    >
      <Plus className="h-3.5 w-3.5" />
    </button>
  );
}

export const nodeTypes = {
  modelCard: CardNode,
  measuresCard: CardNode,
  reportCard: CardNode,
};
