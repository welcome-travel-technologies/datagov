"use client";

import {
  BaseEdge,
  EdgeText,
  getSmoothStepPath,
  useInternalNode,
  type EdgeProps,
} from "@xyflow/react";
import { getEdgeParams } from "@/lib/metrics-canvas/floating-edge";
import type { CanvasEdgeData } from "@/lib/metrics-canvas/types";

type Pt = { x: number; y: number };

function dist(a: Pt, b: Pt): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

/** A point `d` along the segment from `from` toward `to`. */
function along(from: Pt, to: Pt, d: number): Pt {
  const len = dist(from, to) || 1;
  return { x: from.x + ((to.x - from.x) / len) * d, y: from.y + ((to.y - from.y) / len) * d };
}

/** Build a rounded orthogonal SVG path through ELK's route points, plus a label
 *  anchor at the middle segment. Corners are eased with a small quadratic so the
 *  routed edges match the smoothstep look of the live (floating) ones. */
function routedPath(points: Pt[], radius = 10): [string, number, number] {
  let d = `M ${points[0].x},${points[0].y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const p0 = points[i - 1];
    const p1 = points[i];
    const p2 = points[i + 1];
    const r = Math.min(radius, dist(p0, p1) / 2, dist(p1, p2) / 2);
    const a = along(p1, p0, r);
    const b = along(p1, p2, r);
    d += ` L ${a.x},${a.y} Q ${p1.x},${p1.y} ${b.x},${b.y}`;
  }
  const last = points[points.length - 1];
  d += ` L ${last.x},${last.y}`;
  const seg = Math.max(0, Math.floor((points.length - 1) / 2));
  return [d, (points[seg].x + points[seg + 1].x) / 2, (points[seg].y + points[seg + 1].y) / 2];
}

/**
 * Canvas edge with two modes:
 *  - **routed** — when auto-arrange has baked an orthogonal `data.route`, draw
 *    exactly that path so the edge weaves cleanly around the boxes.
 *  - **floating** — otherwise attach to the point on each node's border facing
 *    its partner (computed live), so a hand-dragged edge stays glued with no
 *    looping out of a stale port.
 *
 * Either way it only changes *how* an edge is drawn — never which nodes connect
 * or the arrow direction. Labels use the SVG <EdgeText> (not the HTML
 * EdgeLabelRenderer) so the PNG export still captures every operator pill.
 */
export function FloatingEdge({
  id,
  source,
  target,
  data,
  markerStart,
  markerEnd,
  style,
  label,
  labelStyle,
  labelShowBg,
  labelBgStyle,
  labelBgPadding,
  labelBgBorderRadius,
}: EdgeProps) {
  const sourceNode = useInternalNode(source);
  const targetNode = useInternalNode(target);
  const route = (data as CanvasEdgeData | undefined)?.route;

  let path = "";
  let labelX = 0;
  let labelY = 0;
  if (route && route.length >= 2) {
    [path, labelX, labelY] = routedPath(route);
  } else if (sourceNode && targetNode) {
    const { sx, sy, tx, ty, sourcePos, targetPos } = getEdgeParams(sourceNode, targetNode);
    [path, labelX, labelY] = getSmoothStepPath({
      sourceX: sx,
      sourceY: sy,
      sourcePosition: sourcePos,
      targetX: tx,
      targetY: ty,
      targetPosition: targetPos,
      borderRadius: 10,
    });
  } else {
    return null;
  }

  return (
    <>
      <BaseEdge id={id} path={path} markerStart={markerStart} markerEnd={markerEnd} style={style} />
      {label ? (
        <EdgeText
          x={labelX}
          y={labelY}
          label={label}
          labelStyle={labelStyle}
          labelShowBg={labelShowBg}
          labelBgStyle={labelBgStyle}
          labelBgPadding={labelBgPadding}
          labelBgBorderRadius={labelBgBorderRadius}
        />
      ) : null}
    </>
  );
}

export const edgeTypes = {
  floating: FloatingEdge,
};
