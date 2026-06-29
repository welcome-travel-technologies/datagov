"use client";

import { memo, useState } from "react";
import { Handle, NodeResizer, Position, type NodeProps } from "@xyflow/react";
import { cn } from "@/lib/utils";
import { typeMeta } from "@/lib/metrics-canvas/catalog";
import { buildShapeSVG, hexToRgba } from "@/lib/metrics-canvas/shapes";
import type { CanvasNodeData } from "@/lib/metrics-canvas/types";
import { useCanvasInteraction } from "@/components/metrics-canvas/interaction";

/** Map-wide text scaling. Kept as an inline `font-size` (not a Tailwind class)
 *  so the value is captured verbatim by the PNG export, which clones only the
 *  viewport subtree and would otherwise lose a cascaded CSS variable. */
function fontPx(base: number, scale: number) {
  return { fontSize: `${+(base * scale).toFixed(2)}px` };
}

/** Four connectable ports (one per side). With `ConnectionMode.Loose` on the
 *  pane, any port can be the start or the end of a connection. */
function Ports() {
  const cls =
    "!h-2.5 !w-2.5 !rounded-full !border-2 !border-background !bg-brand opacity-0 transition-opacity group-hover:opacity-100";
  return (
    <>
      <Handle id="top" type="source" position={Position.Top} className={cls} />
      <Handle id="right" type="source" position={Position.Right} className={cls} />
      <Handle id="bottom" type="source" position={Position.Bottom} className={cls} />
      <Handle id="left" type="source" position={Position.Left} className={cls} />
    </>
  );
}

/** Power BI element chip (measure / column / table / page / report / …). */
export const ElementNode = memo(function ElementNode({ data, selected }: NodeProps) {
  const d = data as unknown as CanvasNodeData;
  const meta = typeMeta(d.elementType);
  const color = d.borderColor || meta.color;
  const scale = useCanvasInteraction().fontScale * (d.fontScale ?? 1);
  return (
    <div
      className={cn(
        "group relative flex h-full w-full items-center gap-2 rounded-lg border-2 bg-panel px-3 py-2 shadow-card",
        selected && "ring-2 ring-brand/40",
      )}
      style={{ borderColor: color }}
      title={d.tooltip || d.label}
    >
      <NodeResizer minWidth={120} minHeight={44} isVisible={!!selected} lineClassName="!border-brand" handleClassName="!bg-brand !border-background" />
      <span className="grid h-6 w-6 shrink-0 place-items-center leading-none" style={{ color, ...fontPx(15, scale) }}>
        {meta.icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-semibold leading-tight" style={fontPx(12.5, scale)}>{d.label}</span>
        {d.sub && <span className="block truncate text-faint" style={fontPx(10.5, scale)}>{d.sub}</span>}
      </span>
      {d.meta && (
        <span
          className="shrink-0 rounded bg-brand/10 px-1 py-px font-bold leading-none text-brand"
          style={fontPx(8.5, scale)}
          title={`From catalog: ${d.meta.itemType}`}
        >
          PB
        </span>
      )}
      <Ports />
    </div>
  );
});

/** Freeform SVG shape (cylinder / document / cloud / diamond / ellipse / hexagon). */
export const ShapeNode = memo(function ShapeNode({ data, selected, width, height }: NodeProps) {
  const d = data as unknown as CanvasNodeData;
  const meta = typeMeta(d.elementType);
  const scale = useCanvasInteraction().fontScale * (d.fontScale ?? 1);
  const shape = meta.shape || "ellipse";
  const stroke = d.borderColor || meta.color;
  const fill = hexToRgba(d.fillColor || meta.color, d.fillAlpha ?? 0.12);
  const w = Math.max(20, (width as number | undefined) ?? 140);
  const h = Math.max(20, (height as number | undefined) ?? 96);
  return (
    <div className="group relative h-full w-full" title={d.tooltip || d.label}>
      <NodeResizer minWidth={60} minHeight={40} isVisible={!!selected} lineClassName="!border-brand" handleClassName="!bg-brand !border-background" />
      <svg
        width={w}
        height={h}
        viewBox={`0 0 ${w} ${h}`}
        className={cn("absolute inset-0 overflow-visible", selected && "drop-shadow-[0_0_0_2px_rgba(13,148,136,0.35)]")}
        dangerouslySetInnerHTML={{ __html: buildShapeSVG(shape, w, h, fill, stroke, 2) }}
      />
      <div className="absolute inset-0 flex items-center justify-center px-2 text-center font-medium leading-tight" style={fontPx(12, scale)}>
        {d.label}
      </div>
      <Ports />
    </div>
  );
});

/** Section container — a translucent frame other nodes sit on top of. */
export const ContainerNode = memo(function ContainerNode({ data, selected }: NodeProps) {
  const d = data as unknown as CanvasNodeData;
  const color = d.borderColor || typeMeta(d.elementType).color;
  const scale = useCanvasInteraction().fontScale * (d.fontScale ?? 1);
  return (
    <div
      className={cn("group relative h-full w-full rounded-lg border-2 border-dashed", selected && "ring-2 ring-brand/30")}
      style={{ borderColor: color, background: hexToRgba(color, 0.05) }}
    >
      <NodeResizer minWidth={140} minHeight={100} isVisible={!!selected} lineClassName="!border-brand" handleClassName="!bg-brand !border-background" />
      <div
        className="pointer-events-none absolute left-2.5 top-1.5 font-semibold uppercase tracking-[0.04em]"
        style={{ color, ...fontPx(11, scale) }}
      >
        {d.label}
      </div>
      <Ports />
    </div>
  );
});

/** Text / sticky note — double-click to edit inline. */
export const NoteNode = memo(function NoteNode({ id, data, selected }: NodeProps) {
  const d = data as unknown as CanvasNodeData;
  const sticky = d.elementType === "sticky";
  const { onLabelCommit, fontScale, readOnly } = useCanvasInteraction();
  const scale = fontScale * (d.fontScale ?? 1);
  const [editing, setEditing] = useState(false);

  return (
    <div
      className={cn("group relative h-full w-full", selected && "ring-2 ring-brand/40 rounded")}
      onDoubleClick={readOnly ? undefined : () => setEditing(true)}
    >
      <NodeResizer minWidth={80} minHeight={32} isVisible={!!selected} lineClassName="!border-brand" handleClassName="!bg-brand !border-background" />
      <div
        className={cn(
          "h-full w-full overflow-hidden rounded px-2 py-1.5 leading-snug",
          sticky ? "shadow-card" : "",
        )}
        style={{
          background: sticky ? d.fillColor || "#fff6c2" : "transparent",
          color: d.textColor || (sticky ? "#3a2f00" : "inherit"),
          ...fontPx(12.5, scale),
        }}
      >
        {editing ? (
          <textarea
            autoFocus
            defaultValue={d.label}
            onBlur={(e) => {
              setEditing(false);
              onLabelCommit(id, e.target.value);
            }}
            style={{ fontFamily: "inherit", ...fontPx(12.5, scale) }}
            className="nodrag nopan h-full w-full resize-none bg-transparent outline-none"
          />
        ) : (
          <div className="h-full w-full whitespace-pre-wrap break-words">
            {d.label || (sticky ? "Sticky note" : "Text")}
          </div>
        )}
      </div>
      <Ports />
    </div>
  );
});

export const nodeTypes = {
  element: ElementNode,
  shape: ShapeNode,
  container: ContainerNode,
  note: NoteNode,
};
