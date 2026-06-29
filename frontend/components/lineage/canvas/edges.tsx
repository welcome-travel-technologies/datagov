"use client";

import { BaseEdge, type EdgeProps } from "@xyflow/react";

/**
 * Edge for an intra-card (self-dependent) relationship — e.g. a PowerBI measure
 * that depends on another measure in the same Measures card. Both endpoints sit
 * on the card's right edge, so this draws a clean right-side arc whose reach
 * grows with the vertical span: a loop between adjacent rows barely bulges, one
 * spanning the whole card reaches further out. Stacked loops therefore nest as
 * consistent, non-overlapping arcs instead of wrapping around the card or
 * collapsing into a tangle of identical beziers.
 */
export function SelfLoopEdge({ id, sourceX, sourceY, targetX, targetY, style, markerEnd }: EdgeProps) {
  const x = Math.max(sourceX, targetX); // the card's right edge
  const span = Math.abs(targetY - sourceY);
  const reach = Math.min(120, 18 + span * 0.45); // arc-diagram nesting; capped so big cards don't shoot wide
  const path =
    `M ${sourceX},${sourceY} ` + `C ${x + reach},${sourceY} ${x + reach},${targetY} ${targetX},${targetY}`;
  return <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />;
}

export const edgeTypes = {
  selfLoop: SelfLoopEdge,
};
