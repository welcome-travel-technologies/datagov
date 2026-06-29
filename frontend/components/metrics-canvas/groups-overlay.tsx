"use client";

import { useRef } from "react";
import { ViewportPortal, useReactFlow } from "@xyflow/react";
import { hexToRgba } from "@/lib/metrics-canvas/shapes";
import type { RfNode } from "@/lib/metrics-canvas/serialize";
import type { CanvasGroup } from "@/lib/metrics-canvas/types";

function sizeOf(n: RfNode): { w: number; h: number } {
  const w = (n.width as number | undefined) ?? n.measured?.width ?? 190;
  const h = (n.height as number | undefined) ?? n.measured?.height ?? 60;
  return { w, h };
}

const PAD = 16;
const LABEL_H = 18;

/** Group bounding-box frames drawn (in flow coordinates) behind the nodes.
 *  The name label doubles as a drag handle: grabbing it moves every member
 *  node of the group together (the frame follows since it is derived). */
export function GroupsOverlay({
  groups,
  nodes,
  fontScale = 1,
  selectedId,
  onSelect,
  onDragStart,
  onDrag,
  onDragStop,
}: {
  groups: CanvasGroup[];
  nodes: RfNode[];
  fontScale?: number;
  /** Id of the currently selected group (highlights its frame). */
  selectedId?: string | null;
  /** Click the group label to select it (a click that isn't a drag). */
  onSelect?: (groupId: string) => void;
  /** Called once when a group drag begins (snapshot point for undo). */
  onDragStart?: () => void;
  /** Called on each move with the flow-space delta to apply to members. */
  onDrag?: (groupId: string, dx: number, dy: number) => void;
  /** Called once when a group drag ends (commit point). */
  onDragStop?: () => void;
}) {
  const { getZoom } = useReactFlow();
  const drag = useRef<{ id: string; x: number; y: number; moved: boolean } | null>(null);

  if (!groups.length) return null;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const labelH = Math.round(LABEL_H * fontScale);

  function onPointerDown(e: React.PointerEvent, groupId: string) {
    if (e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = { id: groupId, x: e.clientX, y: e.clientY, moved: false };
  }

  function onPointerMove(e: React.PointerEvent) {
    const d = drag.current;
    if (!d) return;
    const zoom = getZoom() || 1;
    const dx = (e.clientX - d.x) / zoom;
    const dy = (e.clientY - d.y) / zoom;
    if (!dx && !dy) return;
    if (!d.moved) {
      d.moved = true;
      onDragStart?.();
    }
    d.x = e.clientX;
    d.y = e.clientY;
    onDrag?.(d.id, dx, dy);
  }

  function onPointerUp(e: React.PointerEvent) {
    const d = drag.current;
    if (!d) return;
    (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
    drag.current = null;
    // A press that never moved is a click → select the group; otherwise it was
    // a drag → commit the move.
    if (d.moved) onDragStop?.();
    else onSelect?.(d.id);
  }

  return (
    <ViewportPortal>
      <div style={{ position: "absolute", inset: 0, pointerEvents: "none", zIndex: 0 }}>
        {groups.map((g) => {
          const members = g.nodeIds.map((id) => byId.get(id)).filter(Boolean) as RfNode[];
          if (!members.length) return null;
          let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
          for (const n of members) {
            const { w, h } = sizeOf(n);
            minX = Math.min(minX, n.position.x);
            minY = Math.min(minY, n.position.y);
            maxX = Math.max(maxX, n.position.x + w);
            maxY = Math.max(maxY, n.position.y + h);
          }
          const x = minX - PAD;
          const y = minY - PAD - labelH;
          const width = maxX - minX + PAD * 2;
          const height = maxY - minY + PAD * 2 + labelH;
          const isSelected = selectedId === g.id;
          return (
            <div
              key={g.id}
              style={{
                position: "absolute",
                left: x,
                top: y,
                width,
                height,
                // Selected groups get a solid, slightly thicker frame + a soft
                // ring so the current selection reads clearly against siblings.
                border: isSelected ? `2px solid ${g.color}` : `1.5px dashed ${g.color}`,
                background: hexToRgba(g.color, isSelected ? 0.1 : 0.05),
                borderRadius: 10,
                boxShadow: isSelected ? `0 0 0 3px ${hexToRgba(g.color, 0.25)}` : undefined,
              }}
            >
              <span
                onPointerDown={(e) => onPointerDown(e, g.id)}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                title="Click to select · drag to move this group"
                style={{
                  position: "absolute",
                  left: 6,
                  top: 2,
                  padding: "1px 6px",
                  fontSize: 11 * fontScale,
                  fontWeight: 600,
                  color: isSelected ? "#fff" : g.color,
                  background: isSelected ? g.color : hexToRgba(g.color, 0.14),
                  borderRadius: 6,
                  pointerEvents: "auto",
                  cursor: "grab",
                  userSelect: "none",
                  touchAction: "none",
                }}
              >
                {g.name}
              </span>
            </div>
          );
        })}
      </div>
    </ViewportPortal>
  );
}
