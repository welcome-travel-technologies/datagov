"use client";

import {
  BaseEdge,
  EdgeText,
  getSmoothStepPath,
  useInternalNode,
  type EdgeProps,
} from "@xyflow/react";
import { getEdgeParams } from "@/lib/metrics-canvas/floating-edge";

/**
 * Floating edge: attaches to the point on each node's border that faces its
 * partner (computed from geometry, not a fixed port), so connections route
 * cleanly however the boxes are arranged or whichever side they were drawn from
 * — no more looping up and over out of a stale handle. It changes only *how* an
 * edge is drawn, never which nodes are connected or the arrow direction.
 *
 * The path is an orthogonal smoothstep so it matches the map's existing
 * right-angle look. Labels are drawn with the SVG <EdgeText> (not the HTML
 * EdgeLabelRenderer) so the PNG export — which clones only the
 * `.react-flow__viewport` SVG — still captures every operator pill. Label,
 * marker and style props pass straight through, so `decorateEdgeLabel` keeps
 * working unchanged.
 */
export function FloatingEdge({
  id,
  source,
  target,
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
  if (!sourceNode || !targetNode) return null;

  const { sx, sy, tx, ty, sourcePos, targetPos } = getEdgeParams(sourceNode, targetNode);
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX: sx,
    sourceY: sy,
    sourcePosition: sourcePos,
    targetX: tx,
    targetY: ty,
    targetPosition: targetPos,
    borderRadius: 10,
  });

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
